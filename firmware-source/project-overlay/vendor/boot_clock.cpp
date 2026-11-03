#include "intercom/boot_clock.h"
#include <openssl/evp.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <array>
#include <fstream>
#include <iterator>
#include <set>

#ifndef INTERCOM_BOOT_CLOCK_PATH
#define INTERCOM_BOOT_CLOCK_PATH "/tmp/intercom-m56-boot.clock.json"
#endif
namespace intercom::vendor {
namespace {
bool Hex(const std::string& value, std::size_t size) {
  return value.size()==size && value.find_first_not_of("0123456789abcdef")==std::string::npos &&
      value.find_first_not_of('0')!=std::string::npos;
}
bool Digest(const std::string& bytes,std::string& result) {
  std::array<unsigned char,32> digest{};unsigned size=0;
  if(EVP_Digest(bytes.data(),bytes.size(),digest.data(),&size,EVP_sha256(),nullptr)!=1||size!=digest.size())return false;
  static constexpr char digits[]="0123456789abcdef";result.clear();
  for(auto c:digest){result+=digits[c>>4];result+=digits[c&15];}return true;
}
bool SelfDigest(std::string& result) {
  std::ifstream file("/proc/self/exe",std::ios::binary);
  if(!file)return false;
  const std::string bytes{std::istreambuf_iterator<char>(file),{}};
  return !file.bad() && !bytes.empty() && Digest(bytes,result);
}
bool Number(const nlohmann::json& j,const char* key,std::uint64_t& value) {
  if(!j.contains(key)||!j.at(key).is_number_unsigned())return false;
  value=j.at(key).get<std::uint64_t>();return value!=0;
}
bool BootId(const std::string& value,Id& boot) {
  if(value.size()!=36||value[8]!='-'||value[13]!='-'||value[18]!='-'||value[23]!='-')return false;
  std::string hex;for(std::size_t i=0;i<value.size();++i)if(i!=8&&i!=13&&i!=18&&i!=23)hex+=value[i];
  if(!Hex(hex,32))return false;
  for(std::size_t i=0;i<boot.size();++i){
    const auto digit=[](char c){return c<='9'?c-'0':c-'a'+10;};
    boot[i]=static_cast<unsigned char>((digit(hex[2*i])<<4)|digit(hex[2*i+1]));
  }return true;
}
}
bool LoadBootClock(const nlohmann::json& config,const std::string& config_bytes,
                   TlsConfig& tls,std::shared_ptr<HaClockSource>& source,std::string& error) {
  tls.trusted_wall_clock=false;tls.use_ha_clock=true;tls.ha_clock.reset();tls.ha_credentials.reset();source.reset();
  error="HA boot clock admission unavailable";
  try {
    std::uint64_t generation=0,floor=0,rate=0;
    if(config.at("ha_boot_clock_path").get<std::string>()!=INTERCOM_BOOT_CLOCK_PATH||
       !Number(config,"ha_clock_generation",generation)||!Number(config,"ha_clock_floor_utc_us",floor)||
       !Number(config,"ha_clock_rate_ppb",rate)||rate!=1000000)return false;
    const int fd=open(INTERCOM_BOOT_CLOCK_PATH,O_RDONLY|O_NOFOLLOW|O_CLOEXEC);
    if(fd<0)return false;
    struct stat st{};std::string bytes;
    bool ok=fstat(fd,&st)==0&&S_ISREG(st.st_mode)&&st.st_uid==0&&(st.st_mode&07777)==0600&&
        st.st_nlink==1&&st.st_size>0&&st.st_size<=4096;
    if(ok){bytes.resize(static_cast<std::size_t>(st.st_size));std::size_t offset=0;
      while(offset<bytes.size()){const auto count=read(fd,&bytes[offset],bytes.size()-offset);if(count<=0){ok=false;break;}offset+=static_cast<std::size_t>(count);}
      char extra=0;if(ok&&read(fd,&extra,1)!=0)ok=false;
    }
    if(close(fd)!=0)ok=false;
    if(!ok)return false;
    std::set<std::string> seen;
    const auto ticket=nlohmann::json::parse(bytes,[&](int,nlohmann::json::parse_event_t event,nlohmann::json& value){
      if(event==nlohmann::json::parse_event_t::key&&!seen.insert(value.get<std::string>()).second)
        throw std::runtime_error("duplicate clock key");
      return true;
    });
    const std::set<std::string> fields={"schema","boot_id","nonce","boottime_us","ha_utc_low_us","ha_utc_high_us",
      "generation","floor_utc_us","max_rate_ppb","application_sha256","configuration_sha256"};
    std::set<std::string> actual;for(auto it=ticket.begin();it!=ticket.end();++it)actual.insert(it.key());
    std::uint64_t schema=0,ticket_generation=0,ticket_floor=0,ticket_rate=0,anchor=0,low=0,high=0;
    if(actual!=fields||!Number(ticket,"schema",schema)||schema!=1||!Number(ticket,"generation",ticket_generation)||
       !Number(ticket,"floor_utc_us",ticket_floor)||!Number(ticket,"max_rate_ppb",ticket_rate)||
       !Number(ticket,"boottime_us",anchor)||!Number(ticket,"ha_utc_low_us",low)||!Number(ticket,"ha_utc_high_us",high)||
       ticket_generation!=generation||ticket_floor!=floor||ticket_rate!=rate||!Hex(ticket.at("nonce").get<std::string>(),32))return false;
    const auto boot_text=ticket.at("boot_id").get<std::string>();Id boot{};
    std::ifstream boot_file("/proc/sys/kernel/random/boot_id");std::string current_boot;std::getline(boot_file,current_boot);
    if(!boot_file||current_boot!=boot_text||!BootId(boot_text,boot))return false;
    std::string digest;
    if(!SelfDigest(digest)||ticket.at("application_sha256").get<std::string>()!=digest||
       !Digest(config_bytes,digest)||ticket.at("configuration_sha256").get<std::string>()!=digest)return false;
    // /proc/uptime is quantized; retain a conservative 10 ms margin per side.
    if(high<low||low<=10000||high>UINT64_MAX-10000)return false;
    auto candidate=std::make_shared<HaClockSource>(rate,floor,generation,boot);
    if(!candidate->Attest({low-10000,high+10000},anchor))return false;
    // Recheck the named inode before consumption. No ticket survives a launch.
    struct stat named{};
    if(lstat(INTERCOM_BOOT_CLOCK_PATH,&named)!=0||named.st_dev!=st.st_dev||named.st_ino!=st.st_ino||
       unlink(INTERCOM_BOOT_CLOCK_PATH)!=0)return false;
    tls.ha_clock=candidate->Snapshot();tls.ha_credentials=std::make_shared<HaCredentialEpoch>(generation,boot);
    source=std::move(candidate);error.clear();return true;
  }catch(const std::exception&){return false;}
}
}

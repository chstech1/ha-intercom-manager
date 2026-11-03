#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

#include "ha_clock.h"

namespace intercom {

struct TlsConfig {
  std::string ca_file;
  std::string certificate_file;
  std::string key_file;
  std::string peer_uri;
  std::string peer_spki_sha256;
  bool trusted_wall_clock = false;
  bool use_ha_clock = false;
  std::shared_ptr<const HaClockSnapshot> ha_clock;
  std::shared_ptr<HaCredentialEpoch> ha_credentials;
};

enum class IoCode { Done, Again, Closed, Error };

struct IoResult {
  IoCode code;
  std::size_t count = 0;
  bool want_write = false;
};

class TlsStream {
 public:
  TlsStream(int fd, const TlsConfig& config, bool server);
  ~TlsStream();

  TlsStream(const TlsStream&) = delete;
  TlsStream& operator=(const TlsStream&) = delete;
  TlsStream(TlsStream&&) = delete;
  TlsStream& operator=(TlsStream&&) = delete;

  bool Valid() const;
  int FailureCode() const;
  IoResult Handshake();
  IoResult Read(uint8_t* data, std::size_t size);
  IoResult Write(const uint8_t* data, std::size_t size);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace intercom

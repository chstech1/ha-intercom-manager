#include "endpoint_tls.h"

#include <algorithm>
#include <array>
#include <climits>
#include <cstring>

#include <openssl/crypto.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

namespace intercom {
namespace {

constexpr char kTls12Ciphers[] =
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256";
constexpr char kTls13Ciphers[] =
    "TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "TLS_AES_128_GCM_SHA256";

bool IsCanonicalPeerUri(const std::string& value) {
  constexpr char kHa[] = "urn:intercom:ha:";
  constexpr char kEndpoint[] = "urn:intercom:endpoint:";
  const std::size_t prefix = value.compare(0, sizeof(kHa) - 1, kHa) == 0
                                 ? sizeof(kHa) - 1
                                 : value.compare(0, sizeof(kEndpoint) - 1,
                                                 kEndpoint) == 0
                                       ? sizeof(kEndpoint) - 1
                                       : 0;
  if (prefix == 0 || value.size() != prefix + 36) return false;
  for (std::size_t i = 0; i < 36; ++i) {
    const char c = value[prefix + i];
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (c != '-') return false;
    } else if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
      return false;
    }
  }
  return true;
}

bool DecodePin(const std::string& hex, std::array<unsigned char, 32>* out) {
  if (hex.size() != out->size() * 2) return false;
  auto nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
  };
  for (std::size_t i = 0; i < out->size(); ++i) {
    const int hi = nibble(hex[i * 2]);
    const int lo = nibble(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) return false;
    (*out)[i] = static_cast<unsigned char>((hi << 4) | lo);
  }
  return true;
}

bool HasExactUriSan(X509* cert, const std::string& expected) {
  GENERAL_NAMES* names = static_cast<GENERAL_NAMES*>(
      X509_get_ext_d2i(cert, NID_subject_alt_name, nullptr, nullptr));
  if (!names) return false;
  bool match = false;
  int uri_count = 0;
  for (int i = 0; i < sk_GENERAL_NAME_num(names); ++i) {
    const GENERAL_NAME* name = sk_GENERAL_NAME_value(names, i);
    if (name->type != GEN_URI) continue;
    ++uri_count;
    const ASN1_IA5STRING* uri = name->d.uniformResourceIdentifier;
    const unsigned char* bytes = ASN1_STRING_get0_data(uri);
    const int length = ASN1_STRING_length(uri);
    match = length == static_cast<int>(expected.size()) &&
            std::memcmp(bytes, expected.data(), expected.size()) == 0;
  }
  GENERAL_NAMES_free(names);
  return uri_count == 1 && match;
}

bool HasDigitalSignature(X509* cert) {
  ASN1_BIT_STRING* usage = static_cast<ASN1_BIT_STRING*>(
      X509_get_ext_d2i(cert, NID_key_usage, nullptr, nullptr));
  if (!usage) return false;
  bool result = ASN1_BIT_STRING_get_bit(usage, 0) == 1;
  for (int bit=1;bit<9;++bit) result &= ASN1_BIT_STRING_get_bit(usage,bit)==0;
  ASN1_BIT_STRING_free(usage);
  return result;
}

bool IsLeaf(X509* cert) {
  auto* constraint=static_cast<BASIC_CONSTRAINTS*>(X509_get_ext_d2i(cert,NID_basic_constraints,nullptr,nullptr));
  if(!constraint)return false;
  const bool leaf=!constraint->ca && constraint->pathlen==nullptr;
  BASIC_CONSTRAINTS_free(constraint);
  return leaf;
}

bool HasOnlyExpectedTlsEku(X509* cert, bool peer_is_server) {
  EXTENDED_KEY_USAGE* usage = static_cast<EXTENDED_KEY_USAGE*>(
      X509_get_ext_d2i(cert, NID_ext_key_usage, nullptr, nullptr));
  if (!usage) return false;
  const int wanted = peer_is_server ? NID_server_auth : NID_client_auth;
  bool found = false;
  bool inappropriate = false;
  for (int i = 0; i < sk_ASN1_OBJECT_num(usage); ++i) {
    const int nid = OBJ_obj2nid(sk_ASN1_OBJECT_value(usage, i));
    found |= nid == wanted;
    inappropriate |= nid == (peer_is_server ? NID_client_auth : NID_server_auth) ||
                     nid == NID_anyExtendedKeyUsage;
  }
  EXTENDED_KEY_USAGE_free(usage);
  return found && !inappropriate;
}

bool MatchesPin(X509* cert, const std::array<unsigned char, 32>& expected) {
  std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
  std::array<unsigned char,8192> der{};
  X509_PUBKEY* key=X509_get_X509_PUBKEY(cert);
  const int size=i2d_X509_PUBKEY(key,nullptr);
  if(size<=0||static_cast<std::size_t>(size)>der.size())return false;
  auto* output=der.data();
  if(i2d_X509_PUBKEY(key,&output)!=size)return false;
  unsigned int length = 0;
  // X509_pubkey_digest hashes only the key BIT STRING, not SubjectPublicKeyInfo.
  // Pin the complete DER SPKI, including algorithm parameters, as specified.
  if (EVP_Digest(der.data(),static_cast<std::size_t>(size),digest.data(),&length,EVP_sha256(),nullptr) != 1 ||
      length != expected.size()) {
    return false;
  }
  return CRYPTO_memcmp(digest.data(), expected.data(), expected.size()) == 0;
}

IoResult Translate(SSL* ssl, int result, std::size_t count) {
  if (result > 0) return {IoCode::Done, count, false};
  const int error = SSL_get_error(ssl, result);
  if (error == SSL_ERROR_WANT_READ) return {IoCode::Again, 0, false};
  if (error == SSL_ERROR_WANT_WRITE) return {IoCode::Again, 0, true};
  if (error == SSL_ERROR_ZERO_RETURN ||
      (error == SSL_ERROR_SYSCALL && result == 0)) {
    return {IoCode::Closed, 0, false};
  }
  return {IoCode::Error, 0, false};
}

}  // namespace

struct TlsStream::Impl {
  SSL_CTX* context = nullptr;
  SSL* ssl = nullptr;
  std::array<unsigned char, 32> peer_pin{};
  std::string peer_uri;
  bool peer_is_server = false;
  bool handshake_done = false;
  int failure = 0;

  ~Impl() {
    SSL_free(ssl);
    SSL_CTX_free(context);
  }

  bool VerifyPeer() {
    if (SSL_get_verify_result(ssl) != X509_V_OK) {failure=1;return false;}
    X509* cert = SSL_get_peer_certificate(ssl);
    if (!cert) {failure=2;return false;}
    failure=!IsLeaf(cert)?3:!HasExactUriSan(cert,peer_uri)?4:!HasDigitalSignature(cert)?5:
            !HasOnlyExpectedTlsEku(cert,peer_is_server)?6:!MatchesPin(cert,peer_pin)?7:0;
    const bool valid=failure==0;
    X509_free(cert);
    return valid;
  }
};

TlsStream::TlsStream(int fd, const TlsConfig& config, bool server)
    : impl_(new Impl) {
  const char* expected_role = server ? "urn:intercom:ha:"
                                     : "urn:intercom:endpoint:";
  if (fd < 0 || !config.trusted_wall_clock ||
      config.ca_file.empty() || config.certificate_file.empty() ||
      config.key_file.empty() || !IsCanonicalPeerUri(config.peer_uri) ||
      config.peer_uri.compare(0, std::strlen(expected_role), expected_role) != 0 ||
      !DecodePin(config.peer_spki_sha256, &impl_->peer_pin)) {
    impl_.reset();
    return;
  }
  impl_->peer_uri = config.peer_uri;
  impl_->peer_is_server = !server;
  impl_->context = SSL_CTX_new(TLS_method());
  if (!impl_->context) {
    impl_.reset();
    return;
  }

  if (SSL_CTX_set_min_proto_version(impl_->context, TLS1_2_VERSION) != 1 ||
      SSL_CTX_set_max_proto_version(impl_->context, TLS1_3_VERSION) != 1) {
    impl_.reset();
    return;
  }
  SSL_CTX_set_options(impl_->context, SSL_OP_NO_COMPRESSION |
                                          SSL_OP_NO_RENEGOTIATION |
                                          SSL_OP_NO_TICKET);
  SSL_CTX_set_session_cache_mode(impl_->context, SSL_SESS_CACHE_OFF);
  SSL_CTX_set_max_early_data(impl_->context, 0);
  const int verify_mode = SSL_VERIFY_PEER |
                          (server ? SSL_VERIFY_FAIL_IF_NO_PEER_CERT : 0);
  SSL_CTX_set_verify(impl_->context, verify_mode, nullptr);
  SSL_CTX_set_verify_depth(impl_->context, 3);
  SSL_CTX_set_max_cert_list(impl_->context,16384);

  if (SSL_CTX_set_cipher_list(impl_->context, kTls12Ciphers) != 1 ||
      SSL_CTX_set_ciphersuites(impl_->context, kTls13Ciphers) != 1 ||
      SSL_CTX_load_verify_locations(impl_->context, config.ca_file.c_str(),
                                    nullptr) != 1 ||
      SSL_CTX_use_certificate_chain_file(impl_->context,
                                         config.certificate_file.c_str()) != 1 ||
      SSL_CTX_use_PrivateKey_file(impl_->context, config.key_file.c_str(),
                                  SSL_FILETYPE_PEM) != 1 ||
      SSL_CTX_check_private_key(impl_->context) != 1) {
    impl_.reset();
    return;
  }

  impl_->ssl = SSL_new(impl_->context);
  if (!impl_->ssl || SSL_set_fd(impl_->ssl, fd) != 1) {
    impl_.reset();
    return;
  }
  if (server)
    SSL_set_accept_state(impl_->ssl);
  else
    SSL_set_connect_state(impl_->ssl);
  SSL_set_mode(impl_->ssl,
               SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
}

TlsStream::~TlsStream() = default;

bool TlsStream::Valid() const { return impl_ && impl_->ssl; }
int TlsStream::FailureCode() const {return impl_?impl_->failure:8;}

IoResult TlsStream::Handshake() {
  if (!Valid() || impl_->handshake_done) {
    return {impl_ && impl_->handshake_done ? IoCode::Done : IoCode::Error, 0,
            false};
  }
  ERR_clear_error();
  const int result = SSL_do_handshake(impl_->ssl);
  if (result == 1) {
    if (!impl_->VerifyPeer()) return {IoCode::Error, 0, false};
    impl_->handshake_done = true;
    return {IoCode::Done, 0, false};
  }
  return Translate(impl_->ssl, result, 0);
}

IoResult TlsStream::Read(uint8_t* data, std::size_t size) {
  if (!Valid() || !impl_->handshake_done || (!data && size != 0))
    return {IoCode::Error, 0, false};
  if (size == 0) return {IoCode::Done, 0, false};
  const int amount = static_cast<int>(std::min<std::size_t>(size, INT_MAX));
  ERR_clear_error();
  const int result = SSL_read(impl_->ssl, data, amount);
  return Translate(impl_->ssl, result,
                   result > 0 ? static_cast<std::size_t>(result) : 0);
}

IoResult TlsStream::Write(const uint8_t* data, std::size_t size) {
  if (!Valid() || !impl_->handshake_done || (!data && size != 0))
    return {IoCode::Error, 0, false};
  if (size == 0) return {IoCode::Done, 0, false};
  const int amount = static_cast<int>(std::min<std::size_t>(size, INT_MAX));
  ERR_clear_error();
  const int result = SSL_write(impl_->ssl, data, amount);
  return Translate(impl_->ssl, result,
                   result > 0 ? static_cast<std::size_t>(result) : 0);
}

}  // namespace intercom

#pragma once
#include "intercom/endpoint_tls.h"
#include <nlohmann/json.hpp>

namespace intercom::vendor {
// Ordinary root-file custody only. This does not provide protected antirollback.
bool LoadBootClock(const nlohmann::json& config, const std::string& config_bytes,
                   TlsConfig& tls, std::shared_ptr<HaClockSource>& source,
                   std::string& error);
}

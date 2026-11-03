#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

// M4 offline codec only. TLS, direction, ownership, lease and FSM checks belong
// to the caller. Successful syntax parsing never authorizes an audio operation.
namespace intercom {
struct Frame {
    std::uint8_t type = 0;
    std::uint16_t size = 0;
    std::array<std::uint8_t, 2048> body{};
};

inline std::uint16_t U16(const std::uint8_t* p) {
    return static_cast<std::uint16_t>(p[0] | (std::uint16_t{p[1]} << 8));
}
inline std::uint32_t U32(const std::uint8_t* p) {
    return static_cast<std::uint32_t>(p[0]) |
           (static_cast<std::uint32_t>(p[1]) << 8) |
           (static_cast<std::uint32_t>(p[2]) << 16) |
           (static_cast<std::uint32_t>(p[3]) << 24);
}
inline std::uint64_t U64(const std::uint8_t* p) {
    std::uint64_t result = 0;
    for (unsigned i = 0; i < 8; ++i) result |= std::uint64_t{p[i]} << (8 * i);
    return result;
}
inline bool IsZero(const std::uint8_t* p, std::size_t size) {
    for (std::size_t i = 0; i < size; ++i) if (p[i] != 0) return false;
    return true;
}
inline bool Utf8(const std::uint8_t* p, std::size_t size) {
    for (std::size_t i = 0; i < size;) {
        const auto first = p[i++];
        if (first < 0x80) continue;
        unsigned count = 0;
        std::uint32_t code = 0, minimum = 0;
        if (first >= 0xc2 && first <= 0xdf) { count = 1; code = first & 0x1f; minimum = 0x80; }
        else if (first >= 0xe0 && first <= 0xef) { count = 2; code = first & 0x0f; minimum = 0x800; }
        else if (first >= 0xf0 && first <= 0xf4) { count = 3; code = first & 7; minimum = 0x10000; }
        else return false;
        if (size - i < count) return false;
        for (unsigned j = 0; j < count; ++j) {
            if ((p[i] & 0xc0) != 0x80) return false;
            code = (code << 6) | (p[i++] & 0x3f);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return false;
    }
    return true;
}
inline bool SizeAllowed(std::uint8_t type, std::uint16_t size) {
    switch (type) {
        case 1: return size == 1110;                 // AUDIO v2
        case 2: return size >= 71 && size <= 135;   // START
        case 3: case 9: case 13: return size == 66;
        case 4: return size == 80;
        case 5: return size == 96;
        case 6: return size >= 67 && size <= 163;
        case 7: return size == 65;
        case 8: return size == 72;
        case 10: return size >= 65 && size <= 129;
        case 11: return size == 60;
        case 12: return size == 88;
        case 14: return size == 68;
        case 15: return size == 71;
        default: return false;
    }
}
inline bool ValidBody(const Frame& f) {
    if (!SizeAllowed(f.type, f.size)) return false;
    const auto* b = f.body.data();
    const auto mode = [](std::uint8_t v) { return v == 1 || v == 2; };
    const auto reason = [](std::uint16_t v) { return v >= 1 && v <= 13; };
    switch (f.type) {
        case 1: return U16(b + 84) == 512;
        case 2: return mode(b[64]) && b[65] == 1 && U16(b + 66) == 0 && U16(b + 68) == 0 && b[70] <= 64 && f.size == 71 + b[70] && Utf8(b + 71, b[70]);
        case 3: case 9: case 13: return reason(U16(b + 64));
        case 5: return U64(b + 88) >= U64(b + 80);
        case 6: return reason(U16(b + 64)) && b[66] <= 96 && f.size == 67 + b[66] && Utf8(b + 67, b[66]);
        case 7: return (b[64] & ~7u) == 0;
        case 8: return mode(b[64]) && b[65] == 1 && U16(b + 66) <= 255 && U64(b + 64) >> 32 == 0;
        case 10: return std::memcmp(b, "ICM2", 4) == 0 && U16(b + 4) == 2 && U16(b + 6) == 2 && U16(b + 60) == 1 && U16(b + 62) == 0 && b[64] <= 64 && f.size == 65 + b[64] && Utf8(b + 65, b[64]);
        case 11: return std::memcmp(b, "ICM2", 4) == 0 && U16(b + 4) == 2 && U16(b + 6) == 0 && U16(b + 56) == 1 && U16(b + 58) == 0;
        case 12: {
            const auto state = b[64], status_mode = b[65];
            if (state > 6 || U16(b + 66) > 255) return false;
            if (state == 0)
                return status_mode == 0 && IsZero(b + 40, 16) && U64(b + 56) == 0;
            if (IsZero(b + 40, 16) || U64(b + 56) == 0 || !mode(status_mode)) return false;
            return (state != 3 || status_mode == 1) &&
                   (state != 4 || status_mode == 2) &&
                   (state != 5 || status_mode == 2);
        }
        case 15: return b[68] <= 4 && U16(b + 69) <= 255;
        default: return true;
    }
}

// Stateless replay/identity comparison, not authentication or a session FSM.
// Sequence advances only in the caller after all its checks and dispatch pass.
inline bool EnvelopeMatches(const Frame& f,
                            const std::array<std::uint8_t, 16>& connection,
                            const std::array<std::uint8_t, 16>& boot,
                            std::uint64_t next_sequence,
                            const std::array<std::uint8_t, 16>& call,
                            std::uint64_t generation) {
    return ValidBody(f) && f.type != 10 && f.type != 11 &&
           next_sequence != std::numeric_limits<std::uint64_t>::max() &&
           std::memcmp(f.body.data(), connection.data(), 16) == 0 &&
           std::memcmp(f.body.data() + 16, boot.data(), 16) == 0 &&
           U64(f.body.data() + 32) == next_sequence &&
           std::memcmp(f.body.data() + 40, call.data(), 16) == 0 &&
           U64(f.body.data() + 56) == generation;
}
inline bool AudioGenerationMatches(const Frame& f, std::uint32_t mode_generation) {
    return f.type == 1 && ValidBody(f) && U32(f.body.data() + 64) == mode_generation;
}

class StreamParser {
public:
    template<class Consumer>
    bool Feed(const std::uint8_t* bytes, std::size_t size, Consumer&& consumer) {
        if (failed_) return false;
        while (size) {
            if (header_used_ < header_.size()) {
                const auto count = std::min(size, header_.size() - header_used_);
                std::memcpy(header_.data() + header_used_, bytes, count);
                header_used_ += count; bytes += count; size -= count;
                if (header_used_ < header_.size()) continue;
                frame_.type = header_[0]; frame_.size = U16(header_.data() + 1);
                if (!SizeAllowed(frame_.type, frame_.size)) return Fail();
            }
            const auto count = std::min(size, std::size_t{frame_.size} - body_used_);
            std::memcpy(frame_.body.data() + body_used_, bytes, count);
            body_used_ += count; bytes += count; size -= count;
            if (body_used_ == frame_.size) {
                if (!ValidBody(frame_) || !consumer(frame_)) return Fail();
                frame_.body.fill(0); header_used_ = 0; body_used_ = 0;
            }
        }
        return true;
    }
    bool End() { return !failed_ && (header_used_ == 0 || Fail()); }
    bool Failed() const { return failed_; }
private:
    bool Fail() { failed_ = true; frame_.body.fill(0); return false; }
    Frame frame_{};
    std::array<std::uint8_t, 3> header_{};
    std::size_t header_used_ = 0, body_used_ = 0;
    bool failed_ = false;
};
}  // namespace intercom

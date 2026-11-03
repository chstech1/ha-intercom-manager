#pragma once
#include <array>
#include <algorithm>
#include <cstdint>
namespace intercom {
struct StreamVolume {
    std::uint8_t level=100, maximum=100, chime=100;
    std::uint64_t revision=0;
};
inline std::array<std::uint8_t,1024> ApplyStreamGain(const std::array<std::uint8_t,1024>& pcm, unsigned percent) {
    auto out=pcm;
    percent=std::min(percent,100u); // no amplification or signed overflow
    for (std::size_t i=0;i<out.size();i+=2) {
        const auto word=static_cast<std::uint16_t>(pcm[i] | (std::uint16_t{pcm[i+1]}<<8));
        const std::int32_t sample=word<32768 ? word : static_cast<std::int32_t>(word)-65536;
        const auto scaled=static_cast<std::uint16_t>(sample*static_cast<std::int32_t>(percent)/100);
        out[i]=static_cast<std::uint8_t>(scaled);out[i+1]=static_cast<std::uint8_t>(scaled>>8);
    }
    return out;
}
} // namespace intercom

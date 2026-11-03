#pragma once

#include <array>
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace intercom::vendor {

// One worker owns this object. Input timestamps describe the first sample.
class CaptureAccumulator {
 public:
  static constexpr std::size_t kSamples = 512;

  template <class Emit>
  void Push(const std::int16_t* samples, std::size_t count,
            std::uint64_t first_sample_us, std::uint32_t generation,
            bool allowed, Emit emit) {
    if (!allowed || (size_ != 0 && generation != generation_)) Clear();
    if (!allowed) return;
    while (count != 0) {
      if (size_ == 0) {
        oldest_us_ = first_sample_us;
        generation_ = generation;
      }
      const std::size_t take = std::min(count, kSamples - size_);
      std::copy_n(samples, take, samples_.begin() + size_);
      samples += take;
      count -= take;
      size_ += take;
      first_sample_us += static_cast<std::uint64_t>(take) * 1000000 / 16000;
      if (size_ == kSamples) {
        std::array<std::uint8_t, 1024> pcm{};
        std::memcpy(pcm.data(), samples_.data(), pcm.size());
        emit(pcm, generation_, oldest_us_);
        Clear();
      }
    }
  }

  void Clear() { size_ = 0; oldest_us_ = 0; generation_ = 0; }
  std::size_t Size() const { return size_; }

 private:
  std::array<std::int16_t, kSamples> samples_{};
  std::size_t size_ = 0;
  std::uint64_t oldest_us_ = 0;
  std::uint32_t generation_ = 0;
};

}  // namespace intercom::vendor

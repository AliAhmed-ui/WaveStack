// AudioEngine.cpp -- miniaudio-backed playback + decoded-buffer access for WaveStack.
//
// Design notes
//  * The whole file is decoded up front into one interleaved float32 buffer at a
//    fixed sample rate / channel count. Playback just walks that buffer, so the
//    audio callback does no I/O, no allocation and takes no locks.
//  * miniaudio owns the background audio thread (the device data callback), so
//    "playback on a separate thread" needs no extra thread of our own.
//  * Position is an atomic frame cursor minus the device's output latency, so the
//    value Python polls tracks what is audible, not what has been queued.
//  * The buffer is only swapped while the device is stopped, which is what makes
//    the callback's raw-pointer reads safe. Python arrays returned by
//    get_audio_buffer() hold a shared_ptr to the buffer, so they stay valid even
//    after a later load_file().
//  * The equalizer is a miniaudio node graph: buffer reader -> low shelf (bass)
//    -> peaking (mid) -> high shelf (treble) -> endpoint. The device callback
//    pulls the graph, then applies volume. Python only stores target gains in
//    atomics; the audio thread itself ramps the live gains toward them (1 dB per
//    callback) and re-derives the biquad coefficients, so no lock is shared with
//    the callback and a dragged slider never steps the filter (no zipper pops).

#define MINIAUDIO_IMPLEMENTATION
#define MA_NO_ENCODING
#define MA_NO_GENERATION
#include "miniaudio.h"

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace {

constexpr ma_uint32 kSampleRate = 44100;
constexpr ma_uint32 kChannels = 2;

constexpr double kBassHz = 100.0;
constexpr double kMidHz = 1000.0;
constexpr double kTrebleHz = 8000.0;
constexpr double kShelfSlope = 0.9;
constexpr double kMidQ = 0.8;
constexpr float kMaxGainDb = 12.0f;
constexpr float kGainStepDb = 1.0f;  // max change per device callback (~10 ms)

}  // namespace

class AudioEngine;

// Feeds the decoded buffer into the node graph. Always delivers the full request
// (zero-padded past the end) so the graph never stalls; the engine tracks the end.
struct BufferSource {
    ma_data_source_base base;
    AudioEngine* engine;
};

class AudioEngine {
public:
    AudioEngine() {
        ma_device_config cfg = ma_device_config_init(ma_device_type_playback);
        cfg.playback.format = ma_format_f32;
        cfg.playback.channels = kChannels;
        cfg.sampleRate = kSampleRate;
        cfg.performanceProfile = ma_performance_profile_low_latency;
        cfg.dataCallback = &AudioEngine::data_callback;
        cfg.pUserData = this;

        if (ma_device_init(nullptr, &cfg, &device_) != MA_SUCCESS) {
            throw std::runtime_error("AudioEngine: could not open an audio output device");
        }
        device_ready_ = true;
        init_fft_tables();
        try {
            init_graph();
        } catch (...) {
            ma_device_uninit(&device_);
            throw;
        }
    }

    ~AudioEngine() {
        if (device_ready_) ma_device_uninit(&device_);  // stops the device and joins its thread
        if (graph_ready_) {
            ma_hishelf_node_uninit(&treble_node_, nullptr);
            ma_peak_node_uninit(&mid_node_, nullptr);
            ma_loshelf_node_uninit(&bass_node_, nullptr);
            ma_data_source_node_uninit(&source_node_, nullptr);
            ma_data_source_uninit(&source_.base);
            ma_node_graph_uninit(&graph_, nullptr);
        }
    }

    AudioEngine(const AudioEngine&) = delete;
    AudioEngine& operator=(const AudioEngine&) = delete;

    // Decodes the whole file (MP3/WAV/FLAC) to float32 stereo at 44.1 kHz.
    void load_file(const std::string& path) {
        std::lock_guard<std::mutex> lock(control_mutex_);

        ma_decoder_config dcfg = ma_decoder_config_init(ma_format_f32, kChannels, kSampleRate);
        ma_decoder decoder;
        if (ma_decoder_init_file(path.c_str(), &dcfg, &decoder) != MA_SUCCESS) {
            throw std::runtime_error("AudioEngine: cannot decode '" + path + "'");
        }

        auto samples = std::make_shared<std::vector<float>>();
        constexpr ma_uint64 kChunkFrames = 16384;
        ma_uint64 total = 0;
        if (ma_decoder_get_length_in_pcm_frames(&decoder, &total) == MA_SUCCESS && total > 0) {
            samples->reserve(static_cast<size_t>(total) * kChannels);
        }
        std::vector<float> chunk(kChunkFrames * kChannels);
        for (;;) {
            ma_uint64 got = 0;
            ma_result r = ma_decoder_read_pcm_frames(&decoder, chunk.data(), kChunkFrames, &got);
            if (got > 0) samples->insert(samples->end(), chunk.begin(), chunk.begin() + got * kChannels);
            if (r != MA_SUCCESS || got < kChunkFrames) break;
        }
        ma_decoder_uninit(&decoder);

        if (samples->empty()) {
            throw std::runtime_error("AudioEngine: '" + path + "' decoded to zero frames");
        }

        // Quiesce the callback before swapping the buffer it reads.
        stop_device_locked();
        buffer_ = std::move(samples);
        data_ptr_.store(buffer_->data(), std::memory_order_release);
        frames_.store(buffer_->size() / kChannels, std::memory_order_release);
        cursor_.store(0, std::memory_order_release);
        finished_.store(false, std::memory_order_release);
        reset_spectrum();
    }

    void play() {
        std::lock_guard<std::mutex> lock(control_mutex_);
        if (!buffer_) return;
        if (cursor_.load() >= frames_.load()) cursor_.store(0);  // replay after end
        finished_.store(false);
        if (!ma_device_is_started(&device_)) ma_device_start(&device_);
    }

    void pause() {
        std::lock_guard<std::mutex> lock(control_mutex_);
        stop_device_locked();
    }

    void stop() {
        std::lock_guard<std::mutex> lock(control_mutex_);
        stop_device_locked();
        cursor_.store(0);
        finished_.store(false);
        reset_spectrum();
    }

    void seek_ms(double ms) {
        const auto frames = frames_.load();
        double clamped = std::max(0.0, ms);
        auto target = static_cast<uint64_t>(clamped * kSampleRate / 1000.0);
        cursor_.store(std::min<uint64_t>(target, frames));
        finished_.store(false);
    }

    // Audible position: frames handed to the device minus what is still queued in it.
    double get_position_ms() const {
        uint64_t cur = cursor_.load(std::memory_order_acquire);
        if (playing_.load(std::memory_order_acquire)) {
            uint64_t lat = latency_frames_.load(std::memory_order_relaxed);
            cur = cur > lat ? cur - lat : 0;
        }
        return static_cast<double>(cur) * 1000.0 / kSampleRate;
    }

    double get_duration_ms() const {
        return static_cast<double>(frames_.load()) * 1000.0 / kSampleRate;
    }

    bool is_playing() const { return playing_.load(); }

    // True once playback reached the end of the track on its own; cleared by play/stop/seek/load.
    bool is_finished() const { return finished_.load(); }

    // Equalizer gains in dB (clamped to +/-12). Lock-free: the audio thread picks
    // the new target up on its next callback and glides to it.
    void set_bass(float db) { bass_target_.store(clamp_db(db), std::memory_order_relaxed); }
    void set_mid(float db) { mid_target_.store(clamp_db(db), std::memory_order_relaxed); }
    void set_treble(float db) { treble_target_.store(clamp_db(db), std::memory_order_relaxed); }

    void set_volume(float v) { volume_.store(std::clamp(v, 0.0f, 1.0f)); }
    float get_volume() const { return volume_.load(); }

    int sample_rate() const { return static_cast<int>(kSampleRate); }
    int channels() const { return static_cast<int>(kChannels); }

    // Real-time spectrum for the LED visualizer: `bands` log-spaced bars in 0..1,
    // already smoothed (fast attack, slow decay) and scaled by the volume.
    // All the math lives here: Hann window -> FFT -> band peaks -> dB -> smoothing.
    py::array_t<float> get_spectrum(int bands) {
        bands = std::clamp(bands, 1, 256);
        py::array_t<float> result(static_cast<py::ssize_t>(bands));
        float* out = result.mutable_data();
        std::fill(out, out + bands, 0.0f);

        std::shared_ptr<std::vector<float>> buf;
        {
            std::unique_lock<std::mutex> lock(control_mutex_, std::try_to_lock);
            if (!lock.owns_lock()) return result;  // a load is in progress
            buf = buffer_;
        }

        std::lock_guard<std::mutex> lock(spectrum_mutex_);
        if (static_cast<int>(smoothed_.size()) != bands) smoothed_.assign(bands, 0.0f);

        // Audible position, window centred on it.
        const int64_t total = static_cast<int64_t>(frames_.load());
        uint64_t cur = cursor_.load(std::memory_order_acquire);
        uint64_t lat = latency_frames_.load(std::memory_order_relaxed);
        int64_t centre = static_cast<int64_t>(cur > lat ? cur - lat : 0);
        int64_t start = centre - kFftSize / 2;

        std::vector<float> power(kFftSize / 2, 0.0f);
        if (buf && total > 0) {
            for (int i = 0; i < kFftSize; ++i) {
                int64_t f = start + i;
                float mono = 0.0f;
                if (f >= 0 && f < total) mono = 0.5f * ((*buf)[f * kChannels] + (*buf)[f * kChannels + 1]);
                fft_[i] = std::complex<float>(mono * window_[i], 0.0f);
            }
            fft_in_place();
            const float vol = volume_.load(std::memory_order_relaxed);
            // A full-scale sine through a Hann window peaks at kFftSize / 4.
            const float norm = vol / (kFftSize / 4.0f);
            for (int k = 0; k < kFftSize / 2; ++k) power[k] = std::abs(fft_[k]) * norm;
        }

        // Smoothing is time-based so it looks the same at any poll rate.
        auto now = std::chrono::steady_clock::now();
        float dt = std::chrono::duration<float>(now - last_spectrum_time_).count();
        last_spectrum_time_ = now;
        dt = std::clamp(dt, 0.001f, 0.25f);
        const float attack = 1.0f - std::exp(-dt / 0.030f);
        const float decay = 1.0f - std::exp(-dt / 0.140f);

        const float lo = 40.0f, hi = std::min(16000.0f, kSampleRate / 2.0f);
        const float bin_hz = static_cast<float>(kSampleRate) / kFftSize;
        for (int b = 0; b < bands; ++b) {
            float f0 = lo * std::pow(hi / lo, static_cast<float>(b) / bands);
            float f1 = lo * std::pow(hi / lo, static_cast<float>(b + 1) / bands);
            int k0 = std::max(1, static_cast<int>(std::floor(f0 / bin_hz)));
            int k1 = std::min(kFftSize / 2 - 1, std::max(k0, static_cast<int>(std::ceil(f1 / bin_hz))));
            float peak = 0.0f;
            for (int k = k0; k <= k1; ++k) peak = std::max(peak, power[k]);

            // dB, with +3 dB/octave tilt so treble isn't buried under bass.
            float centre_hz = std::sqrt(f0 * f1);
            float db = 20.0f * std::log10(peak + 1e-9f) + 3.0f * std::log2(centre_hz / 1000.0f);
            float target = std::clamp((db + 50.0f) / 42.0f, 0.0f, 1.0f);

            float& s = smoothed_[b];
            s += (target - s) * (target > s ? attack : decay);
            out[b] = s;
        }
        return result;
    }

    // Zero-copy, read-only (frames, channels) float32 view of the decoded audio.
    // The array's base owns a shared_ptr, so it outlives later load_file() calls.
    py::array_t<float> get_audio_buffer() const {
        std::shared_ptr<std::vector<float>> buf;
        {
            std::lock_guard<std::mutex> lock(control_mutex_);
            buf = buffer_;
        }
        if (!buf) return py::array_t<float>(std::vector<py::ssize_t>{0, static_cast<py::ssize_t>(kChannels)});

        auto* holder = new std::shared_ptr<std::vector<float>>(buf);
        py::capsule owner(holder, [](void* p) {
            delete static_cast<std::shared_ptr<std::vector<float>>*>(p);
        });
        const auto frames = static_cast<py::ssize_t>(buf->size() / kChannels);
        py::array_t<float> arr(
            {frames, static_cast<py::ssize_t>(kChannels)},
            {static_cast<py::ssize_t>(kChannels * sizeof(float)), static_cast<py::ssize_t>(sizeof(float))},
            buf->data(), owner);
        py::detail::array_proxy(arr.ptr())->flags &= ~py::detail::npy_api::NPY_ARRAY_WRITEABLE_;
        return arr;
    }

private:
    static constexpr int kFftSize = 2048;

    static float clamp_db(float db) {
        if (!std::isfinite(db)) return 0.0f;
        return std::clamp(db, -kMaxGainDb, kMaxGainDb);
    }

    static const ma_data_source_vtable* source_vtable() {
        static const ma_data_source_vtable vt = {
            &AudioEngine::source_read, &AudioEngine::source_seek, &AudioEngine::source_format,
            nullptr, nullptr, nullptr, 0};
        return &vt;
    }

    static ma_result source_read(ma_data_source* ds, void* out, ma_uint64 frame_count, ma_uint64* frames_read) {
        AudioEngine* self = static_cast<BufferSource*>(ds)->engine;
        auto* dst = static_cast<float*>(out);

        const float* src = self->data_ptr_.load(std::memory_order_acquire);
        const uint64_t total = self->frames_.load(std::memory_order_acquire);
        const uint64_t cur = self->cursor_.load(std::memory_order_acquire);
        const uint64_t n = std::min<uint64_t>(frame_count, total > cur ? total - cur : 0);

        if (src && n) std::copy(src + cur * kChannels, src + (cur + n) * kChannels, dst);
        std::fill(dst + n * kChannels, dst + frame_count * kChannels, 0.0f);

        self->cursor_.store(cur + n, std::memory_order_release);
        if (n < frame_count && src) {
            self->playing_.store(false, std::memory_order_release);
            self->finished_.store(true, std::memory_order_release);
        }
        if (frames_read) *frames_read = frame_count;
        return MA_SUCCESS;
    }

    static ma_result source_seek(ma_data_source*, ma_uint64) { return MA_NOT_IMPLEMENTED; }

    static ma_result source_format(ma_data_source*, ma_format* format, ma_uint32* channels,
                                   ma_uint32* rate, ma_channel*, size_t) {
        if (format) *format = ma_format_f32;
        if (channels) *channels = kChannels;
        if (rate) *rate = kSampleRate;
        return MA_SUCCESS;
    }

    void init_graph() {
        ma_node_graph_config gcfg = ma_node_graph_config_init(kChannels);
        if (ma_node_graph_init(&gcfg, nullptr, &graph_) != MA_SUCCESS) {
            throw std::runtime_error("AudioEngine: could not create the DSP graph");
        }
        graph_ready_ = true;

        ma_data_source_config scfg = ma_data_source_config_init();
        scfg.vtable = source_vtable();
        source_.engine = this;
        if (ma_data_source_init(&scfg, &source_.base) != MA_SUCCESS) {
            throw std::runtime_error("AudioEngine: could not create the buffer source");
        }
        ma_data_source_node_config ncfg = ma_data_source_node_config_init(&source_.base);
        ma_loshelf_node_config bcfg = ma_loshelf_node_config_init(kChannels, kSampleRate, 0.0, kShelfSlope, kBassHz);
        ma_peak_node_config mcfg = ma_peak_node_config_init(kChannels, kSampleRate, 0.0, kMidQ, kMidHz);
        ma_hishelf_node_config tcfg = ma_hishelf_node_config_init(kChannels, kSampleRate, 0.0, kShelfSlope, kTrebleHz);
        bass_cfg_ = bcfg.loshelf;
        mid_cfg_ = mcfg.peak;
        treble_cfg_ = tcfg.hishelf;

        if (ma_data_source_node_init(&graph_, &ncfg, nullptr, &source_node_) != MA_SUCCESS ||
            ma_loshelf_node_init(&graph_, &bcfg, nullptr, &bass_node_) != MA_SUCCESS ||
            ma_peak_node_init(&graph_, &mcfg, nullptr, &mid_node_) != MA_SUCCESS ||
            ma_hishelf_node_init(&graph_, &tcfg, nullptr, &treble_node_) != MA_SUCCESS) {
            throw std::runtime_error("AudioEngine: could not create the equalizer nodes");
        }
        ma_node_attach_output_bus(&source_node_, 0, &bass_node_, 0);
        ma_node_attach_output_bus(&bass_node_, 0, &mid_node_, 0);
        ma_node_attach_output_bus(&mid_node_, 0, &treble_node_, 0);
        ma_node_attach_output_bus(&treble_node_, 0, ma_node_graph_get_endpoint(&graph_), 0);
    }

    // Audio thread only: move a live gain one step toward its target and, if it
    // moved, recompute that filter's coefficients (history is kept by reinit).
    static bool glide(float& live, float target) {
        if (live == target) return false;
        float d = target - live;
        live = std::fabs(d) <= kGainStepDb ? target : live + (d > 0 ? kGainStepDb : -kGainStepDb);
        return true;
    }

    void update_eq_gains() {
        if (glide(bass_live_, bass_target_.load(std::memory_order_relaxed))) {
            bass_cfg_.gainDB = bass_live_;
            ma_loshelf_node_reinit(&bass_cfg_, &bass_node_);
        }
        if (glide(mid_live_, mid_target_.load(std::memory_order_relaxed))) {
            mid_cfg_.gainDB = mid_live_;
            ma_peak_node_reinit(&mid_cfg_, &mid_node_);
        }
        if (glide(treble_live_, treble_target_.load(std::memory_order_relaxed))) {
            treble_cfg_.gainDB = treble_live_;
            ma_hishelf_node_reinit(&treble_cfg_, &treble_node_);
        }
    }

    void init_fft_tables() {
        window_.resize(kFftSize);
        for (int i = 0; i < kFftSize; ++i) {
            window_[i] = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI) * i / (kFftSize - 1)));
        }
        twiddle_.resize(kFftSize / 2);
        for (int i = 0; i < kFftSize / 2; ++i) {
            float a = -2.0f * static_cast<float>(M_PI) * i / kFftSize;
            twiddle_[i] = std::complex<float>(std::cos(a), std::sin(a));
        }
        fft_.assign(kFftSize, {});
    }

    // Iterative radix-2 Cooley-Tukey on fft_.
    void fft_in_place() {
        for (int i = 1, j = 0; i < kFftSize; ++i) {
            int bit = kFftSize >> 1;
            for (; j & bit; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) std::swap(fft_[i], fft_[j]);
        }
        for (int len = 2; len <= kFftSize; len <<= 1) {
            int step = kFftSize / len;
            for (int i = 0; i < kFftSize; i += len) {
                for (int k = 0; k < len / 2; ++k) {
                    std::complex<float> u = fft_[i + k];
                    std::complex<float> v = fft_[i + k + len / 2] * twiddle_[k * step];
                    fft_[i + k] = u + v;
                    fft_[i + k + len / 2] = u - v;
                }
            }
        }
    }

    void reset_spectrum() {
        std::lock_guard<std::mutex> lock(spectrum_mutex_);
        std::fill(smoothed_.begin(), smoothed_.end(), 0.0f);
        last_spectrum_time_ = std::chrono::steady_clock::now();
    }
    void stop_device_locked() {
        if (ma_device_is_started(&device_)) ma_device_stop(&device_);  // blocks until callback exits
        playing_.store(false, std::memory_order_release);
    }

    static void data_callback(ma_device* dev, void* out, const void*, ma_uint32 frame_count) {
        auto* self = static_cast<AudioEngine*>(dev->pUserData);
        auto* dst = static_cast<float*>(out);

        self->playing_.store(true, std::memory_order_release);
        self->update_eq_gains();

        ma_uint64 got = 0;
        ma_node_graph_read_pcm_frames(&self->graph_, dst, frame_count, &got);
        if (got < frame_count) std::fill(dst + got * kChannels, dst + static_cast<size_t>(frame_count) * kChannels, 0.0f);

        const float vol = self->volume_.load(std::memory_order_relaxed);
        for (ma_uint32 i = 0; i < frame_count * kChannels; ++i) dst[i] *= vol;

        self->latency_frames_.store(
            static_cast<uint64_t>(dev->playback.internalPeriodSizeInFrames) * dev->playback.internalPeriods *
                kSampleRate / std::max<ma_uint32>(dev->playback.internalSampleRate, 1),
            std::memory_order_relaxed);
    }

    ma_device device_{};
    bool device_ready_ = false;

    ma_node_graph graph_{};
    bool graph_ready_ = false;
    BufferSource source_{};
    ma_data_source_node source_node_{};
    ma_loshelf_node bass_node_{};
    ma_peak_node mid_node_{};
    ma_hishelf_node treble_node_{};
    ma_loshelf_config bass_cfg_{};
    ma_peak_config mid_cfg_{};
    ma_hishelf_config treble_cfg_{};
    // Audio-thread-only live gains; the *_target_ atomics are written by Python.
    float bass_live_ = 0.0f, mid_live_ = 0.0f, treble_live_ = 0.0f;
    std::atomic<float> bass_target_{0.0f}, mid_target_{0.0f}, treble_target_{0.0f};

    mutable std::mutex control_mutex_;  // guards buffer_ and device start/stop, never held in the callback
    std::shared_ptr<std::vector<float>> buffer_;

    std::atomic<const float*> data_ptr_{nullptr};
    std::atomic<uint64_t> frames_{0};
    std::atomic<uint64_t> cursor_{0};
    std::atomic<uint64_t> latency_frames_{0};
    std::atomic<float> volume_{1.0f};
    std::atomic<bool> playing_{false};
    std::atomic<bool> finished_{false};

    std::mutex spectrum_mutex_;  // spectrum scratch state; never touched by the audio callback
    std::vector<float> window_;
    std::vector<std::complex<float>> twiddle_, fft_;
    std::vector<float> smoothed_;
    std::chrono::steady_clock::time_point last_spectrum_time_ = std::chrono::steady_clock::now();
};

// Device-free helper for the offline DSP pass: decodes a mono float32 slice at an
// arbitrary sample rate. Returns an empty array if the file cannot be decoded.
py::array_t<float> decode_file(const std::string& path, int sample_rate, double offset_s, double duration_s) {
    std::vector<float> out;
    {
        py::gil_scoped_release release;
        ma_decoder_config cfg = ma_decoder_config_init(ma_format_f32, 1, static_cast<ma_uint32>(sample_rate));
        ma_decoder dec;
        if (ma_decoder_init_file(path.c_str(), &cfg, &dec) == MA_SUCCESS) {
            bool ok = true;
            if (offset_s > 0.0) {
                ok = ma_decoder_seek_to_pcm_frame(&dec, static_cast<ma_uint64>(offset_s * sample_rate)) == MA_SUCCESS;
            }
            if (ok) {
                ma_uint64 want = duration_s > 0.0 ? static_cast<ma_uint64>(duration_s * sample_rate) : 0;
                std::vector<float> chunk(16384);
                while (want == 0 || out.size() < want) {
                    ma_uint64 ask = chunk.size();
                    if (want) ask = std::min<ma_uint64>(ask, want - out.size());
                    ma_uint64 got = 0;
                    ma_result r = ma_decoder_read_pcm_frames(&dec, chunk.data(), ask, &got);
                    out.insert(out.end(), chunk.begin(), chunk.begin() + got);
                    if (r != MA_SUCCESS || got < ask) break;
                }
            }
            ma_decoder_uninit(&dec);
        }
    }
    py::array_t<float> arr(static_cast<py::ssize_t>(out.size()));
    std::copy(out.begin(), out.end(), arr.mutable_data());
    return arr;
}

PYBIND11_MODULE(wavestack_audio, m) {
    m.doc() = "WaveStack native audio engine (miniaudio)";

    m.def("decode_file", &decode_file, py::arg("path"), py::arg("sample_rate") = 22050,
          py::arg("offset_s") = 0.0, py::arg("duration_s") = 0.0);

    py::class_<AudioEngine>(m, "AudioEngine")
        .def(py::init<>())
        .def("load_file", &AudioEngine::load_file, py::arg("path"), py::call_guard<py::gil_scoped_release>())
        .def("play", &AudioEngine::play, py::call_guard<py::gil_scoped_release>())
        .def("pause", &AudioEngine::pause, py::call_guard<py::gil_scoped_release>())
        .def("stop", &AudioEngine::stop, py::call_guard<py::gil_scoped_release>())
        .def("seek_ms", &AudioEngine::seek_ms, py::arg("ms"))
        .def("get_position_ms", &AudioEngine::get_position_ms)
        .def("get_duration_ms", &AudioEngine::get_duration_ms)
        .def("is_playing", &AudioEngine::is_playing)
        .def("is_finished", &AudioEngine::is_finished)
        .def("set_volume", &AudioEngine::set_volume, py::arg("volume"))
        .def("get_volume", &AudioEngine::get_volume)
        .def("set_bass", &AudioEngine::set_bass, py::arg("db"))
        .def("set_mid", &AudioEngine::set_mid, py::arg("db"))
        .def("set_treble", &AudioEngine::set_treble, py::arg("db"))
        .def_property_readonly("sample_rate", &AudioEngine::sample_rate)
        .def_property_readonly("channels", &AudioEngine::channels)
        .def("get_spectrum", &AudioEngine::get_spectrum, py::arg("bands"))
        .def("get_audio_buffer", &AudioEngine::get_audio_buffer);
}

# Provenance and licensing

This development alpha contains the project's Home Assistant integration, JavaScript/HTML frontend and eight-file public firmware kit, exported from its 0.4.3 source. It preserves the internal `intercom_m3_poc` domain for compatibility. Repository: https://github.com/chstech1/ha-intercom-manager.

Project-authored integration, frontend, helpers, attention tone, icon and project modifications are licensed under Apache-2.0; see [LICENSE](LICENSE). Upstream and embedded third-party components retain the licenses and notices described below.

The public firmware kit includes a modified ThirdReality-derived application and startup script, project-authored startup/recovery helpers and a project-generated attention tone. It does not include the whole SDK, Buildroot tree, kernel, private PKI, household configuration or target shared libraries. The application is dynamically linked to libraries supplied by the existing device image. See [THIRD_PARTY.json](THIRD_PARTY.json) for the bounded provenance inventory.

ThirdReality's repository at `bf9f6838c432685ea8d6b01e9197e9eeedf59426` and C++ application package declare Apache-2.0. Modified application code adds profile4 intercom, privacy/lifecycle hooks and Home Assistant boot-bound time authority; modified S99ha-speaker adds paired bootstrap and monitor adoption. TensorFlow microfrontend source carries Copyright 2018/2019 The TensorFlow Authors and Apache-2.0 notices. The included [Apache-2.0 text](LICENSES/Apache-2.0.txt) preserves that license.

Compiled-in KissFFT carries Mark Borgerding's BSD-3-Clause notice; nlohmann/json 3.11.3 declares MIT (Copyright 2013-2023 Niels Lohmann). Vendor API .proto definitions have no file license header; ESPHome's pinned upstream license assigns files other than its listed C/C++ runtime extensions to MIT. Protobuf-generated C++ comes from those definitions; its extension alone is not evidence that ESPHome runtime C++ was copied. [Additional notices](LICENSES/Additional-notices.txt) preserve the ESPHome MIT, nlohmann MIT, KissFFT BSD and Protobuf BSD texts. This bounded attribution read found no copied ESPHome runtime C/C++ source in the public kit; it does not audit the whole device image.

The attention sound is synthesized by the project's generator as a 200ms 880Hz tone, not copied from vendor recordings. Its reproduced bytes match the bundled sound. Project-authored helpers, the tone, frontend and integration use the project Apache-2.0 license.

Home Assistant and its installed libraries supply the integration runtime dependencies, including AsyncSSH; HACS supplies code delivery. Their source and dependency packages are not vendored here and retain their respective licenses.

The icon is an original geometric drawing generated for this distribution, rather than a Home Assistant or speaker-vendor logo. It uses the project Apache-2.0 license.

The firmware application is a prebuilt AArch64 ELF identified by SHA-256 in [THIRD_PARTY.json](THIRD_PARTY.json) and the firmware catalog. Its vendor base is the pinned ThirdReality revision linked there, with the modifications described above. This HACS repository does not include the modified C++ source/build toolchain and does not claim a standalone reproducible firmware build. Replacing the firmware kit requires a separately reviewed build and matching catalog pins.

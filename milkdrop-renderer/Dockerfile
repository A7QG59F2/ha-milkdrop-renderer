ARG BUILD_FROM
FROM $BUILD_FROM

# Chromium draws Milkdrop (butterchurn, WebGL 2) on the GPU through Mesa's
# Vulkan drivers, or on the CPU with SwiftShader when that fails; aiohttp
# serves the page, takes the audio devices' audio and streams the frames;
# Squeezelite is the optional built-in Music Assistant player that feeds it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium python3-aiohttp squeezelite fonts-dejavu-core ca-certificates curl \
        mesa-vulkan-drivers libvulkan1 libgl1-mesa-dri libegl1 \
    && rm -rf /var/lib/apt/lists/*

# Milkdrop engine and presets, pinned (same versions and hashes as install.sh)
ARG BUTTERCHURN_VERSION=2.6.7
ARG BUTTERCHURN_SHA256=4e67421bc18d48fac4a6ff4e69e2778f770737fbdd4e323438da154085b4818d
ARG BC_PRESETS_VERSION=2.4.7
ARG BC_PRESETS_SHA256=136c746836aef6dff8a1f6a93ede48c7bbe4da27d2f44de6b59ebc56ffefca10
RUN mkdir -p /app/www \
    && curl -fsSL -o /app/www/butterchurn.min.js \
        "https://cdn.jsdelivr.net/npm/butterchurn@${BUTTERCHURN_VERSION}/lib/butterchurn.min.js" \
    && curl -fsSL -o /app/www/butterchurnPresets.min.js \
        "https://cdn.jsdelivr.net/npm/butterchurn-presets@${BC_PRESETS_VERSION}/lib/butterchurnPresets.min.js" \
    && echo "${BUTTERCHURN_SHA256}  /app/www/butterchurn.min.js" | sha256sum -c - \
    && echo "${BC_PRESETS_SHA256}  /app/www/butterchurnPresets.min.js" | sha256sum -c -

COPY rootfs /
CMD ["python3", "-u", "/app/renderer.py"]

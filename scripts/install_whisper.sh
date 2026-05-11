#!/bin/bash
# Build rmorse/whisper.cpp (stream-pcm) with optional GGML GPU backends.
#
# whisper.cpp pulls in ggml; GPU is disabled unless you set the matching GGML_* option:
#   NVIDIA:  -DGGML_CUDA=ON   (needs CUDA toolkit: nvcc + dev libs, not just the driver)
#   Vulkan:  -DGGML_VULKAN=ON
#   AMD:     -DGGML_HIP=ON    (ROCm)
#
# Override backend with WHISPER_GPU=cuda|vulkan|hip|cpu (default: auto).
# "auto" uses CUDA when nvidia-smi works, otherwise CPU-only.
#
# WHISPER_CPP_HOME is the whisper.cpp checkout path (default: $PWD/whisper.cpp).
# The parent directory is used as the clone destination.

set -xe

WHISPER_GPU="${WHISPER_GPU:-auto}"
WHISPER_CPP_HOME="${WHISPER_CPP_HOME:-$PWD/whisper.cpp}"
PARENT="$(dirname "$WHISPER_CPP_HOME")"
REPONAME="$(basename "$WHISPER_CPP_HOME")"
mkdir -p "$PARENT"
cd "$PARENT"

CMAKE_GPU_ARGS=()

case "$WHISPER_GPU" in
auto)
	if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
		CMAKE_GPU_ARGS=(-DGGML_CUDA=ON)
	fi
	;;
cuda)
	CMAKE_GPU_ARGS=(-DGGML_CUDA=ON)
	;;
vulkan)
	CMAKE_GPU_ARGS=(-DGGML_VULKAN=ON)
	;;
hip)
	CMAKE_GPU_ARGS=(-DGGML_HIP=ON)
	;;
cpu)
	CMAKE_GPU_ARGS=()
	;;
*)
	echo "WHISPER_GPU must be auto, cuda, vulkan, hip, or cpu (got: ${WHISPER_GPU})" >&2
	exit 1
	;;
esac

git clone https://github.com/rmorse/whisper.cpp.git "$REPONAME"
cd "$REPONAME"
git checkout stream-pcm
cmake -B build "${CMAKE_GPU_ARGS[@]}"
cmake --build build --config Release -j
./models/download-ggml-model.sh large-v3-turbo-q8_0

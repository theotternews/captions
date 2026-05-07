#!bash

set -xe

pactl \
    load-module \
    module-null-sink \
    sink_name=whisper_sink \
    sink_properties=device.description=WhisperLoopback

pactl \
    load-module \
    module-combine-sink \
    slaves=@DEFAULT_SINK@,whisper_sink \
    sink_name=caption_sink \
    sink_properties=device.description=Captions

pactl \
    move-sink-input \
    $(pactl list short sink-inputs | head -1 | awk '{print $1;}') \
    caption_sink

# ffmpeg \
#     -loglevel quiet \
#     -f pulse -i whisper_sink.monitor \
#     -ar 16000 -ac 1 \
#     -f wav - \
#     | \
#     ./build/bin/whisper-stream-pcm \
#         -m ./models/ggml-base.bin \
#         --format s16 \
#         --sample-rate 16000 \
#         --step 1000 \
#         --length 10000 \
#         --keep 500

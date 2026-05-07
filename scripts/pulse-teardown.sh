#!bash

set -xe

pactl \
    unload-module \
    module-null-sink

pactl \
    unload-module \
    module-combine-sink \

#!/usr/bin/bash

# Error out if there is less than 1 argument
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <Python_image> [command]"
    echo "Example: $0 python:3.14.0a2-slim bash"
    exit 1
fi

# We will get a standard Python image from the input,
# so that we don't need to hard code one in a Dockerfile
IMAGE_NAME=$1

echo "=== Starting $IMAGE_NAME (especially those which have no AppImage yet) ==="
echo "After seeing the bash prompt, run the following to test:"
echo "    apt update && apt install -y gcc libffi-dev  # Needed in Python 3.14.0a2-slim"
echo "    pip install -e ."
echo "    pytest --capture=no -s tests/chosen_test_file.py"
docker run --rm -it \
    --privileged \
    -w /home -v $PWD:/home \
    $IMAGE_NAME \
    $2


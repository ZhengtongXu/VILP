#!/bin/bash

set -e

source ~/miniforge3/etc/profile.d/conda.sh
conda activate vilpenv

mkdir third_party
cd third_party
git clone https://github.com/real-stanford/diffusion_policy.git
cd diffusion_policy
pip install -e .
cd ..
cd ..
pip install -e .
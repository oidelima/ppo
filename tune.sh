#!/usr/bin/env bash

my_ip=$(ip route get 8.8.8.8 | awk -F"src " 'NR==1{split($2,a," ");print a[1]}')

nice python ppo/main.py \
  --cuda-deterministic \
  --log-dir=/home/ethanbro/tune_results \
  --run-id=tune/maiden \
  --num-processes="300" \
  \
  --redis-address "$my_ip:6379" \
  --tune \
  --quiet \
  \
  --instruction="AnswerDoor" \
  --instruction="AvoidDog" \
  --instruction="ComfortBaby" \
  --instruction="KillFlies" \
  --instruction="MakeFire" \
  --instruction="WatchBaby" \
  --test "WatchBaby"  "KillFlies" "MakeFire" \
  --test "AnswerDoor"  "KillFlies" "AvoidDog" \
  --test "AnswerDoor"  "MakeFire" "AvoidDog" \
  --n-active-instructions="3" \
  --time-limit="30" \
  \
  --eval-interval="100" \
  --log-interval="10" \
  --save-interval="300" \
  \
  --env="" \
  --num-batch="-1" \
  --num-steps="-1" \
  --seed="-1" \
  --entropy-coef="-1" \
  --hidden-size="-1" \
  --num-layers="-1" \
  --learning-rate="-1" \
  --ppo-epoch="-1"
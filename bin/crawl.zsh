#!/usr/bin/env zsh

dir=$(zsh ${0:a:h}/get_dir.zsh $1)
zsh ${0:a:h}/check_dir.zsh $dir || exit 1
tag="${2:-eval_rewards}"
crawl_cmd="crawl $dir --tag='$tag'"
echo $crawl_cmd
eval ${crawl_cmd}

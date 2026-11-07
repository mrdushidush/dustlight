#!/usr/bin/env bash
# Rejects staged files that look like game data or assets derived from it, and
# any file over 5 MB (PROJECT_BRIEF §8). Upstream's own assets under the exempt
# paths are allowed. During a merge, a file whose content is identical to the
# merged-in commit came from upstream and is allowed too.
#
# Keep the extension list in sync with the Dustlight block in .gitignore.

set -u

banned='\.(dat|frm|fr[0-5]|pal|msg|map|acm|int|lip|aaf|fon|mve|gam|pro|lst|sve|sav|rix|png|jpe?g|gif|bmp|tga|webp|dds|ktx2?|exr|psd|wav|ogg|mp3|flac|mp4|webm|avi|onnx|pth|pt|safetensors|ckpt|gguf|sqlite|db)$'
exempt='^(docs/images|files|os|sfall_testing)/'
max_bytes=$((5 * 1024 * 1024))

merging=0
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
    merging=1
fi

from_merged_commit() {
    [ "$merging" -eq 1 ] || return 1
    local theirs ours
    theirs=$(git rev-parse -q --verify "MERGE_HEAD:$1" 2>/dev/null) || return 1
    ours=$(git rev-parse -q --verify ":$1" 2>/dev/null) || return 1
    [ "$theirs" = "$ours" ]
}

fail=0
shopt -s nocasematch
while IFS= read -r path; do
    [ -z "$path" ] && continue
    from_merged_commit "$path" && continue

    if [[ ! "$path" =~ $exempt ]] && [[ "$path" =~ $banned ]]; then
        echo "blocked (game data or derived asset): $path"
        fail=1
    fi

    size=$(git cat-file -s ":$path")
    if [ "$size" -gt "$max_bytes" ]; then
        echo "blocked (larger than 5 MB): $path"
        fail=1
    fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

if [ "$fail" -ne 0 ]; then
    echo "Commit rejected by tools/git-hooks/check-staged.sh (PROJECT_BRIEF §8)."
    echo "Game data and anything derived from it must stay outside the repo."
    exit 1
fi

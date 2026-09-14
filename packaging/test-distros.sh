#!/usr/bin/env bash
# Smoke-test the newest AppImage in dist/ on other distros with Podman (rootless, no sudo needed).
#
# In each container it installs the libraries a normal desktop already has, then checks that the
# AppImage starts, reads settings, lists any system libraries its Qt can't find, and keeps the settings
# window open (offscreen) for a few seconds without errors.
#
#   packaging/test-distros.sh                       # all distros below
#   packaging/test-distros.sh ubuntu:22.04          # just one
set -uo pipefail

PROJECT="$(dirname "$(dirname "$(realpath "${BASH_SOURCE[0]}")")")"
APPIMAGE="$(ls -t "$PROJECT"/dist/The_Tux_InVader-*-x86_64.AppImage 2>/dev/null | head -1)"
[ -n "$APPIMAGE" ] || { echo "No AppImage in dist/. Run packaging/build-appimage.sh first." >&2; exit 1; }

DEBIAN_LIBS="libegl1 libgl1 libfontconfig1 libxkbcommon0 libdbus-1-3"
declare -A PREPARE=(
    [ubuntu:22.04]="apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $DEBIAN_LIBS libglib2.0-0"
    [ubuntu:24.04]="apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $DEBIAN_LIBS libglib2.0-0t64"
    [debian:12]="apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $DEBIAN_LIBS libglib2.0-0"
    [fedora:latest]="dnf install -y -q mesa-libEGL mesa-libGL fontconfig libxkbcommon dbus-libs glib2 findutils"
    [archlinux:latest]="pacman -Sy --noconfirm --quiet libglvnd fontconfig libxkbcommon dbus glib2"
)
DISTROS=("$@")
[ ${#DISTROS[@]} -gt 0 ] || DISTROS=(ubuntu:22.04 ubuntu:24.04 debian:12 fedora:latest archlinux:latest)

CHECK='
set -u
cp /dist/app.AppImage /tmp/app.AppImage
cd /tmp
export APPIMAGE_EXTRACT_AND_RUN=1 QT_QPA_PLATFORM=offscreen HOME=/tmp/home
mkdir -p "$HOME"
failed=0
./app.AppImage --version || failed=1
./app.AppImage config check >/dev/null && echo "config check: ok" || failed=1
./app.AppImage --appimage-extract >/dev/null
missing=$(find squashfs-root -path "*PyQt6*" -name "*.so*" -exec ldd {} \; 2>/dev/null | grep "not found" | sort -u)
if [ -n "$missing" ]; then echo "libraries Qt cannot find:"; echo "$missing"; else echo "libraries: all found"; fi
timeout 8 ./app.AppImage settings >out.txt 2>&1
code=$?
if [ "$code" = 124 ] && ! grep -qE "Traceback|Could not load|core dumped" out.txt; then
    echo "settings window: ran 8 s without errors"
else
    echo "settings window: FAILED (exit $code)"; cat out.txt; failed=1
fi
exit $failed
'

results=()
for distro in "${DISTROS[@]}"; do
    echo "===== $distro"
    if podman run --rm --security-opt label=disable -v "$APPIMAGE:/dist/app.AppImage:ro" \
            "docker.io/library/$distro" bash -c "(${PREPARE[$distro]}) >/dev/null 2>&1; $CHECK"; then
        results+=("$distro: passed")
    else
        results+=("$distro: FAILED")
    fi
done
echo "===== summary ($(basename "$APPIMAGE"))"
printf '%s\n' "${results[@]}"

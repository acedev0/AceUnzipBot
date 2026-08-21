{ pkgs }: {
  deps = [
    pkgs.python312
    pkgs.ffmpeg
    pkgs.p7zip
    pkgs.zstd
    pkgs.gcc
    pkgs.pkg-config
    pkgs.rustc
    pkgs.cargo
  ];
}

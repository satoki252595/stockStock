{
  description = "jp-stock-data-pipeline 開発環境 (Python 3.12 + uv / Worker は Node + pnpm)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "aarch64-darwin" "x86_64-linux" ] (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        # uv が入れる numpy/pandas/pyarrow/lxml 等の manylinux wheel は実行時に
        # libstdc++.so.6 等の共有ライブラリを要求する。素の Nix devShell には無いため
        # Linux CI で `ImportError: libstdc++.so.6: cannot open shared object file`
        # になる（macOS は wheel が dylib を同梱するので不要）。LD_LIBRARY_PATH で補う。
        wheelLibs = pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib # libstdc++.so.6 / libgcc_s.so.1 (numpy/pandas/pyarrow)
          pkgs.zlib # libz.so.1
          pkgs.libxml2 # lxml
          pkgs.libxslt # lxml
          pkgs.openssl # 一部 wheel の libssl/libcrypto
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.uv
            # 配信 Worker (worker/) は TypeScript。wrangler を pnpm 経由で使う。
            # グローバルへ入れず nix devShell に閉じ込める (CLAUDE.md の方針)。
            pkgs.nodejs_22
            pkgs.pnpm
          ];
          # uv には nix の Python を使わせる（環境の再現性を nix 側で固定）
          shellHook = ''
            export UV_PYTHON="${pkgs.python312}/bin/python3.12"
            export UV_PYTHON_DOWNLOADS=never
          '' + pkgs.lib.optionalString pkgs.stdenv.isLinux ''
            export LD_LIBRARY_PATH="${wheelLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
          '';
        };
      });
}

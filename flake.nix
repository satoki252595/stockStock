{
  description = "jp-stock-data-pipeline 開発環境 (Python 3.12 + uv)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "aarch64-darwin" "x86_64-linux" ] (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.uv
          ];
          # uv には nix の Python を使わせる（環境の再現性を nix 側で固定）
          shellHook = ''
            export UV_PYTHON="${pkgs.python312}/bin/python3.12"
            export UV_PYTHON_DOWNLOADS=never
          '';
        };
      });
}

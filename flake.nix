{
  description = "EasyWallbox Free2Move Python Mqtt Manager / Monitor";

  inputs = {
    flake-parts.url = "github:hercules-ci/flake-parts";
    nixpkgs.url = "github:nixos/nixpkgs/nixos-25.05";
  };

  outputs = inputs @ {flake-parts, ...}:
    flake-parts.lib.mkFlake {inherit inputs;} {
      debug = true;
      imports = [];
      systems = ["x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin"];
      perSystem = {
        inputs',
        pkgs,
        ...
      }: let
        pythonPackages = pkgs.python312Packages;
        propagatedBuildInputs = with pythonPackages;
          [
            (pythonPackages.callPackage ./nix/paho.nix {})
          ]
          ++ lib.optional (pkgs.stdenv.isLinux) bleak
          ++ lib.optionals (pkgs.stdenv.isDarwin) [
            (pythonPackages.callPackage ./nix/pyobjc-framework-corebluetooth.nix {})
            (pythonPackages.callPackage ./nix/pyobjc-framework-libdispatch.nix {})
            (
            pythonPackages.buildPythonPackage rec {
              pname = "bleak";
              version = bleak.version; #"0.22.3";
              format = "wheel";

              src = fetchPypi {
                inherit pname version format;
                hash = "sha256-HmKp9eDBhIJubJBuNB2KylN5PkWW7q9OCxkeespcRhw=";
                dist = "py3";
                python = "py3";
                platform = "any";
              };
            }
          )
          pyobjc-core];

        easywallbox = pythonPackages.buildPythonApplication {
          inherit propagatedBuildInputs;

          pname = "easywallbox";
          version = "0.1.0";
          src = ./.;
          pyproject = true;

          build-system = [
            pythonPackages.setuptools
          ];

          doCheck = false;
        };
      in {
        formatter = pkgs.alejandra;

        packages.default = easywallbox;

        devShells.default = pkgs.mkShellNoCC {
          inherit propagatedBuildInputs;

          # shellHook = ''
          #   export PYTHONPATH=".:$PYTHONPATH"
          # '';
        };
      };
    };
}

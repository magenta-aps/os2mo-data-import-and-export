# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs = {nixpkgs, ...}: let
    forAllSystems = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
  in {
    # `nix fmt`
    formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.alejandra);

    # `nix develop`
    devShells = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      default = pkgs.mkShell {
        packages = [
          pkgs.python311
          pkgs.poetry

          # Pre-commit hooks use `language: system`, so ruff/mypy must be
          # available on PATH.
          pkgs.ruff
          pkgs.mypy
          pkgs.pre-commit

          # pyodbc
          pkgs.unixODBC

          # pymssql
          pkgs.freetds

          # requests-kerberos
          pkgs.krb5

          # mysqlclient
          pkgs.mariadb-connector-c

          # lxml
          pkgs.libxml2
          pkgs.libxml2.dev
          pkgs.libxslt
          pkgs.libxslt.dev

          # psycopg2-binary (prebuilt, but kept for safety)
          pkgs.libpq
          pkgs.libpq.pg_config

          # Convenience utilities
          pkgs.jq
        ];

        shellHook = ''
          poetry env use ${pkgs.python311}/bin/python3.11
          eval $(poetry env activate)
          poetry install --no-root
        '';
      };
    });
  };
}

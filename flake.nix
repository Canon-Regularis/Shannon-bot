{
  description = "Development environment for Shannon bot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forEachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forEachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312;
          postgres = pkgs.postgresql_17;
          shannon-db-start = pkgs.writeShellApplication {
            name = "shannon-db-start";
            runtimeInputs = [
              pkgs.coreutils
              pkgs.gnugrep
              postgres
            ];
            text = ''
              export PGDATA="''${PGDATA:-$PWD/.nix-postgres}"
              export PGHOST="''${PGHOST:-localhost}"
              export PGPORT="''${PGPORT:-5433}"
              export PGUSER="''${PGUSER:-shannon}"
              export PGPASSWORD="''${PGPASSWORD:-shannon}"

              if [ ! -d "$PGDATA" ]; then
                pwfile="$(mktemp)"
                trap 'rm -f "$pwfile"' EXIT
                printf '%s\n' "$PGPASSWORD" > "$pwfile"
                initdb --username="$PGUSER" --pwfile="$pwfile" --auth-local=trust --auth-host=scram-sha-256 "$PGDATA"
                {
                  printf '%s\n' "listen_addresses = '127.0.0.1'"
                  printf '%s\n' "port = $PGPORT"
                  printf '%s\n' "unix_socket_directories = '$PGDATA'"
                } >> "$PGDATA/postgresql.conf"
              elif ! grep -q '^unix_socket_directories =' "$PGDATA/postgresql.conf"; then
                printf '%s\n' "unix_socket_directories = '$PGDATA'" >> "$PGDATA/postgresql.conf"
              fi

              if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
                pg_ctl -D "$PGDATA" -l "$PGDATA/postgres.log" start
              fi

              createdb --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" shannon 2>/dev/null || true
              createdb --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" shannon_test 2>/dev/null || true
            '';
          };
          shannon-db-stop = pkgs.writeShellApplication {
            name = "shannon-db-stop";
            runtimeInputs = [ postgres ];
            text = ''
              export PGDATA="''${PGDATA:-$PWD/.nix-postgres}"
              pg_ctl -D "$PGDATA" stop
            '';
          };
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [
              pkgs.bashInteractive
              pkgs.docker
              pkgs.docker-compose
              pkgs.git
              pkgs.nodejs_22
              pkgs.openssl
              pkgs.pkg-config
              pkgs.uv
              postgres
              python
              shannon-db-start
              shannon-db-stop
            ];

            env = {
              LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
              UV_PYTHON = "${python}/bin/python";
              UV_PYTHON_DOWNLOADS = "never";
              SHANNON_DATABASE_URL = "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon";
              SHANNON_TEST_DATABASE_URL = "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon_test";
            };

            shellHook = ''
              export PGDATA="$PWD/.nix-postgres"
              export PGHOST="localhost"
              export PGPORT="5433"

              printf '%s\n' "Shannon dev shell: run 'uv sync --extra dev --locked' to install Python dependencies."
              printf '%s\n' "For a Nix-provided local PostgreSQL, run 'shannon-db-start'."
            '';
          };
        }
      );
    };
}

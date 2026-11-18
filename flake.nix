{
  description = "shannon-bot: a development shell, so that `nix develop` is the whole install (#175)";

  # nixpkgs-unstable rather than nixos-unstable. The NixOS branch moves once Linux passes its tests
  # and nothing waits for macOS, so a Mac can find Node or PostgreSQL missing from the binary cache
  # and spend an hour compiling it. This branch waits for both. flake.lock says which commit.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;

      # No x86_64-darwin. Nixpkgs 26.05 was the last release that built for Intel Macs, and on
      # unstable evaluating anything for one is an error rather than a warning.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      forAllSystems = f: lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});

      # The port docker-compose.yml publishes and SHANNON_DATABASE_URL's default names, so the app
      # and alembic reach this database without a line of .env.
      port = "5433";

      # Every uv an astral-sh/setup-uv step in CI is told to install. The shell's has to be that
      # one, or "it passed in nix develop" and "it passed in CI" are two different claims. Only a
      # line that is the bare `version:` key counts, so a `python-version:` pinned to a patch
      # release, or a pin commented out, is never mistaken for uv's.
      ciUv = lib.unique (
        lib.concatLists (
          builtins.filter builtins.isList (
            builtins.split "\n *version: \"([0-9]+\\.[0-9]+\\.[0-9]+)\"" (
              builtins.readFile ./.github/workflows/ci.yml
            )
          )
        )
      );

      shannonDb =
        pkgs:
        pkgs.writeShellApplication {
          name = "shannon-db";
          runtimeInputs = [
            pkgs.postgresql_17
            pkgs.coreutils
          ];
          text = ''
            : "''${PGDATA:?shannon-db is for inside nix develop, which says where the data lives}"
            port=${port}
            log="$(dirname "$PGDATA")/postgres.log"

            usage() {
              echo "usage: shannon-db start | stop | status | reset | psql [psql arguments]" >&2
              exit 2
            }

            not_root() {
              if [ "$(id -u)" = 0 ]; then
                echo "shannon-db: PostgreSQL will not run as root. Enter the shell as an ordinary user." >&2
                exit 1
              fi
            }

            running() { pg_ctl --pgdata="$PGDATA" status >/dev/null 2>&1; }

            # Bash's own /dev/tcp, so asking needs nothing the shell does not already have.
            taken() { (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; }

            sql() {
              psql --host=localhost --port="$port" --username=shannon --no-password \
                --no-psqlrc --quiet --tuples-only --no-align "$@"
            }

            initialise() {
              if [ -f "$PGDATA/PG_VERSION" ]; then return; fi
              mkdir -p "$PGDATA"
              # Trust is safe only because of listen_addresses below: loopback and nothing else.
              # The compose database listens on every interface with a password everybody knows,
              # so this is the narrower of the two. C collation, which is what the alpine image CI
              # tests against has in effect.
              initdb --pgdata="$PGDATA" --username=shannon --auth=trust \
                --encoding=UTF8 --locale=C >/dev/null
              # No Unix socket. Everything here, the app and the tests go over localhost, and a
              # socket brings two ways to stop PostgreSQL starting at all: a checkout deep enough
              # to pass the path limit (103 bytes on macOS), and a checkout moved after this ran,
              # since the directory would be written down here for good.
              {
                echo "port = $port"
                echo "listen_addresses = 'localhost'"
                echo "unix_socket_directories = '''"
              } >>"$PGDATA/postgresql.conf"
            }

            databases() {
              for db in shannon shannon_test; do
                if [ -z "$(sql --dbname=postgres --command="SELECT 1 FROM pg_database WHERE datname = '$db'")" ]; then
                  createdb --host=localhost --port="$port" --username=shannon --no-password "$db"
                fi
              done
            }

            occupied() {
              what="something that is not PostgreSQL"
              if pg_isready --quiet --host=127.0.0.1 --port="$port"; then what="another PostgreSQL"; fi
              {
                echo "shannon-db: $what is already listening on localhost:$port."
                echo
                echo "Most likely the compose database, which publishes the same port so that the URL in"
                echo ".env.example reaches either one. Stop it and run this again:"
                echo
                echo "    docker compose stop db"
                echo
                echo "Or keep it. The app and alembic work against it as they are, but it has no"
                echo "shannon_test database, so the integration tests fail until it has one:"
                echo
                echo "    docker compose exec db createdb -U shannon shannon_test"
              } >&2
              exit 1
            }

            start() {
              if running; then
                databases
                echo "already running on localhost:$port"
                return
              fi
              if taken; then occupied; fi
              initialise
              pg_ctl --pgdata="$PGDATA" --log="$log" --wait --silent start
              databases
              echo "PostgreSQL ${pkgs.postgresql_17.version} on localhost:$port, data in $PGDATA"
            }

            stop() {
              if running; then
                pg_ctl --pgdata="$PGDATA" --wait --silent --mode=fast stop
                echo "stopped"
              else
                echo "not running"
              fi
            }

            status() {
              if running; then
                echo "running on localhost:$port, data in $PGDATA"
              elif taken; then
                echo "stopped, and something else holds localhost:$port"
                exit 3
              else
                echo "stopped"
                exit 3
              fi
            }

            reset() {
              # Only ever the directory nix develop names, whatever PGDATA has been set to since.
              case "$PGDATA" in
                */.nix/postgres) ;;
                *)
                  echo "shannon-db: will not delete $PGDATA" >&2
                  exit 1
                  ;;
              esac
              stop
              rm -rf "$PGDATA" "$log"
              start
            }

            case "''${1:-}" in
              # Refused before any of them runs rather than inside start, because pg_ctl will not
              # answer root either: as root `running` would say no, and reset would delete the
              # data from under a server somebody else started.
              start | stop | status | reset)
                not_root
                "$1"
                ;;
              psql)
                shift
                # Defaults through psql's own variables rather than flags, so that every form psql
                # takes still works: a database name, -c, -d, or nothing at all.
                PGHOST=localhost PGPORT="$port" PGUSER=shannon PGDATABASE=shannon exec psql "$@"
                ;;
              *) usage ;;
            esac
          '';
        };

      devShell =
        pkgs: python:
        pkgs.mkShell {
          name = "shannon-bot-python${python.pythonVersion}";

          packages = [
            python
            pkgs.uv
            # Pyright from PyPI is a Node program. It runs the node on PATH, and without one it
            # downloads its own, which does not run on NixOS.
            pkgs.nodejs
            pkgs.postgresql_17
            (shannonDb pkgs)
            pkgs.git
            pkgs.gitleaks
            pkgs.openssl
            pkgs.curl
            pkgs.jq
            pkgs.nixfmt
          ];

          env = {
            # uv runs the interpreter this shell brings and no other. One it downloaded itself
            # expects a /lib that NixOS does not have.
            UV_PYTHON = python.interpreter;
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON_PREFERENCE = "only-system";
          }
          // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
            # Wheels from PyPI expect libstdc++ and libgcc_s where an ordinary distribution keeps
            # them, which is nowhere Nix's loader looks: greenlet, pydantic-core and watchfiles
            # fail to import without this. Linux only, since a macOS wheel links the system's own.
            # It reaches every program started in the shell, not Python alone, which the README
            # says, since a distribution's own C++ program on an older glibc can then refuse.
            LD_LIBRARY_PATH = lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
            ];
          };

          shellHook = ''
            # Python's setup hook prepares the environment for a package build: PYTHONPATH from
            # the inputs, SOURCE_DATE_EPOCH, and a fixed PYTHONHASHSEED among others. A development
            # shell is not a build, and the seed above all would make set order the same on every
            # run here while CI and production randomise it.
            unset PYTHONPATH SOURCE_DATE_EPOCH PYTHONHASHSEED DETERMINISTIC_BUILD PYTHONNOUSERSITE
            root="$PWD"
            until { [ -f "$root/flake.nix" ] && [ -f "$root/pyproject.toml" ]; } || [ "$root" = / ]; do
              root="$(dirname "$root")"
            done
            if [ "$root" = / ]; then
              echo "nix develop: not inside the shannon-bot checkout, so nothing was synced" >&2
            else
              # Everything this shell writes lives under .nix/, and .venv stays the non-Nix path's.
              # An environment built here runs only with this shell's LD_LIBRARY_PATH, so sharing
              # one would break `uv run` outside it for anybody on Linux.
              export PGDATA="$root/.nix/postgres"
              export UV_PROJECT_ENVIRONMENT="$root/.nix/venv/${python.pythonVersion}"
              # A cache of its own: a wheel built here from an sdist links Nix's glibc and must not
              # be handed to a uv outside the shell. Next to the environments, so uv hardlinks.
              export UV_CACHE_DIR="$root/.nix/uv-cache"
              # Only where it is unset. Empty is the documented way to skip the integration tier.
              export SHANNON_TEST_DATABASE_URL="''${SHANNON_TEST_DATABASE_URL-postgresql+asyncpg://shannon:shannon@localhost:${port}/shannon_test}"
              if [ ! -d "$UV_PROJECT_ENVIRONMENT" ]; then
                echo "nix develop: installing into .nix/venv/${python.pythonVersion}, once" >&2
              fi
              if uv sync --project "$root" --extra dev --locked --quiet; then
                ${lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
                  # Ruff's wheel carries a program rather than a Python module, and the program
                  # names /lib64's loader, which NixOS and a Nix container do not have: it fails to
                  # start with "No such file or directory". A module needs nothing, since the
                  # shell's own Python loads it. So any program the environment gained that names
                  # a loader outside the store is pointed at this shell's, each time uv puts one in.
                  for exe in "$UV_PROJECT_ENVIRONMENT"/bin/*; do
                    loader="$(${pkgs.patchelf}/bin/patchelf --print-interpreter "$exe" 2>/dev/null)" || continue
                    case "$loader" in
                      /nix/store/*) ;;
                      *) ${pkgs.patchelf}/bin/patchelf --set-interpreter ${pkgs.stdenv.cc.bintools.dynamicLinker} "$exe" ;;
                    esac
                  done
                ''}
                # shellcheck disable=SC1091
                . "$UV_PROJECT_ENVIRONMENT/bin/activate"
              else
                echo "nix develop: uv sync --extra dev --locked failed. Run it to see why." >&2
              fi
            fi
            case $- in
              *i*) echo "Python ${python.version}, uv ${pkgs.uv.version}, PostgreSQL ${pkgs.postgresql_17.version}. shannon-db start for the database." ;;
            esac
          '';
        };
    in
    {
      devShells = forAllSystems (
        _system: pkgs:
        let
          shells = lib.mapAttrs (_: devShell pkgs) {
            py312 = pkgs.python312;
            py313 = pkgs.python313;
            py314 = pkgs.python314;
            py315 = pkgs.python315;
          };
        in
        shells // { default = shells.py314; }
      );

      # `nix flake check` on its own only evaluates a devShell. Listing them here makes it build
      # them, which fetches everything they hold and runs shellcheck over shannon-db.
      checks = forAllSystems (
        system: pkgs:
        lib.mapAttrs' (name: lib.nameValuePair "shell-${name}") (
          removeAttrs self.devShells.${system} [ "default" ]
        )
        // {
          nixfmt = pkgs.runCommand "flake-nix-is-formatted" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
            nixfmt --check ${./flake.nix}
            touch "$out"
          '';

          uv-is-the-one-ci-pins =
            assert lib.assertMsg (ciUv == [ pkgs.uv.version ])
              "flake.lock brings uv ${pkgs.uv.version} and .github/workflows/ci.yml pins ${lib.concatStringsSep ", " ciUv}. Move them together.";
            pkgs.runCommand "uv-is-the-one-ci-pins" { } ''touch "$out"'';
        }
      );

      # nixfmt-tree rather than nixfmt: `nix fmt` with no arguments hands nixfmt nothing to read,
      # and nixfmt then waits on stdin.
      formatter = forAllSystems (_system: pkgs: pkgs.nixfmt-tree);
    };
}

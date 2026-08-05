{
  lib,
  stdenv,
  replaceVars,
  python3Packages,
  libunistring,
  harfbuzz,
  fontconfig,
  pkg-config,
  ncurses,
  imagemagick,
  libstartup_notification,
  libGL,
  libx11,
  libxrandr,
  libxinerama,
  libxcursor,
  libxkbcommon,
  libxi,
  libxext,
  wayland-protocols,
  wayland,
  xxhash,
  nerd-fonts,
  lcms2,
  librsync,
  openssl,
  installShellFiles,
  dbus,
  libcanberra,
  libicns,
  wayland-scanner,
  libpng,
  python3,
  zlib,
  simde,
  go_1_26,
  buildGo126Module,
  makeBinaryWrapper,
  darwin,
  cairo,
  shader-slang,
}:

with python3Packages;
buildPythonApplication rec {
  pname = "kitty";
  version = "0.48.2";
  pyproject = false;

  # Built from this working tree, not a release tarball. Build outputs and VCS
  # metadata are filtered out so that editing C sources does not invalidate the
  # fixed-output goModules derivation.
  src =
    let
      # Build outputs and VCS metadata, matched by path rather than by name.
      # By name would also drop kitty/fonts, which is a Python package the
      # build needs, and kitty/shaders.
      excludedPaths = [
        ".git"
        ".jj"
        "build"
        "fonts"
        "shaders"
        "linux-package"
        "kitty.app"
        "result"
      ];
      # Caches, which turn up at any depth.
      excludedNames = [
        "__pycache__"
        ".ruff_cache"
        ".cache"
      ];
      root = toString ./.;
    in
    lib.cleanSourceWith {
      name = "kitty-source";
      src = ./.;
      filter =
        path: type:
        let
          base = baseNameOf path;
          rel = lib.removePrefix "${root}/" (toString path);
        in
        !(builtins.elem rel excludedPaths)
        && !(builtins.elem base excludedNames)
        && !(lib.hasSuffix ".so" base)
        && !(lib.hasSuffix ".pyc" base)
        && !(lib.hasSuffix ".o" base);
    };

  goModules =
    (buildGo126Module {
      pname = "kitty-go-modules";
      inherit src version;
      vendorHash = "sha256-12d6+MX/fijASzj4svdc8+bjjUmnkS1lQ4uGPTove7I=";
    }).goModules;

  buildInputs = [
    harfbuzz
    ncurses
    simde
    lcms2
    librsync
    matplotlib
    openssl.dev
    xxhash
  ]
  ++ lib.optionals stdenv.hostPlatform.isDarwin [
    libpng
    python3
    zlib
  ]
  ++ lib.optionals stdenv.hostPlatform.isLinux [
    fontconfig
    libunistring
    libcanberra
    libx11
    libxrandr
    libxinerama
    libxcursor
    libxkbcommon
    libxi
    libxext
    wayland-protocols
    wayland
    dbus
    libGL
    cairo
  ];

  nativeBuildInputs = [
    installShellFiles
    ncurses
    pkg-config
    sphinx
    furo
    sphinx-copybutton
    sphinxext-opengraph
    # docs/conf.py loads sphinx_design; it dropped sphinx_inline_tabs.
    sphinx-design
    go_1_26
    fontconfig
    makeBinaryWrapper
    # This tree builds its GPU shaders through slangc at build time
    # (kitty/shaders/slang.py), unlike the 0.48.2 release tarball.
    shader-slang
  ]
  ++ lib.optionals stdenv.hostPlatform.isDarwin [
    imagemagick
    libicns # For the png2icns tool.
    darwin.autoSignDarwinBinariesHook
  ]
  ++ lib.optionals stdenv.hostPlatform.isLinux [
    wayland-scanner
  ];

  depsBuildBuild = [ pkg-config ];

  outputs = [
    "out"
    "terminfo"
    "shell_integration"
    "kitten"
  ];

  hardeningDisable = [
    # causes redefinition of _FORTIFY_SOURCE
    "fortify3"
  ];

  env = {
    CGO_ENABLED = 0;
    GOFLAGS = "-trimpath";
    GOTOOLCHAIN = "local";
  };

  configurePhase = ''
    export GOCACHE=$TMPDIR/go-cache
    export GOPATH="$TMPDIR/go"
    export GOPROXY=off
    cp -r --reflink=auto $goModules vendor
  '';

  buildPhase =
    let
      commonOptions = ''
        --update-check-interval=0 \
        --shell-integration=enabled\ no-rc
      '';
      darwinOptions = ''
        --disable-link-time-optimization \
        ${commonOptions}
      '';
    in
    ''
      runHook preBuild

      # Add the font by hand because fontconfig does not find it on darwin
      mkdir ./fonts/
      cp "${nerd-fonts.symbols-only}/share/fonts/truetype/NerdFonts/Symbols/SymbolsNerdFontMono-Regular.ttf" ./fonts/

      ${
        if stdenv.hostPlatform.isDarwin then
          ''
            ${python.pythonOnBuildForHost.interpreter} setup.py build ${darwinOptions}
            make docs
            ${python.pythonOnBuildForHost.interpreter} setup.py kitty.app ${darwinOptions}
          ''
        else
          ''
            ${python.pythonOnBuildForHost.interpreter} setup.py linux-package \
            --egl-library='${lib.getLib libGL}/lib/libEGL.so.1' \
            --startup-notification-library='${libstartup_notification}/lib/libstartup-notification-1.so' \
            --canberra-library='${libcanberra}/lib/libcanberra.so' \
            --fontconfig-library='${fontconfig.lib}/lib/libfontconfig.so' \
            ${commonOptions}
            ${python.pythonOnBuildForHost.interpreter} setup.py build-launcher
          ''
      }
      runHook postBuild
    '';

  # The upstream suite is dominated by shell-integration and ssh tests that
  # need a lot of sandbox coaxing and say nothing about this packaging.
  doCheck = false;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    mkdir -p "$kitten/bin"
    ${
      if stdenv.hostPlatform.isDarwin then
        ''
          mkdir "$out/bin"
          ln -s ../Applications/kitty.app/Contents/MacOS/kitty "$out/bin/kitty"
          ln -s ../Applications/kitty.app/Contents/MacOS/kitten "$out/bin/kitten"
          cp ./kitty.app/Contents/MacOS/kitten "$kitten/bin/kitten"
          mkdir "$out/Applications"
          cp -r kitty.app "$out/Applications/kitty.app"

          installManPage 'docs/_build/man/kitty.1'
        ''
      else
        ''
          cp -r linux-package/{bin,share,lib} "$out"
          cp linux-package/bin/kitten "$kitten/bin/kitten"
        ''
    }

    # dereference the `kitty` symlink to make sure the actual executable
    # is wrapped on macOS as well (and not just the symlink)
    wrapProgram $(realpath "$out/bin/kitty") --suffix PATH : "$out/bin:${
      lib.makeBinPath [
        imagemagick
        ncurses.dev
      ]
    }"

    installShellCompletion --cmd kitty \
      --bash <("$out/bin/kitty" +complete setup bash) \
      --fish <("$out/bin/kitty" +complete setup fish2) \
      --zsh  <("$out/bin/kitty" +complete setup zsh)

    terminfo_src=${
      if stdenv.hostPlatform.isDarwin then
        ''"$out/Applications/kitty.app/Contents/Resources/terminfo"''
      else
        "$out/share/terminfo"
    }

    mkdir -p $terminfo/share
    mv "$terminfo_src" $terminfo/share/terminfo

    mkdir -p "$out/nix-support"
    echo "$terminfo" >> $out/nix-support/propagated-user-env-packages

    cp -r 'shell-integration' "$shell_integration"

    runHook postInstall
  '';

  meta = {
    homepage = "https://github.com/kovidgoyal/kitty";
    description = "Fast, feature-rich, GPU based terminal emulator";
    license = lib.licenses.gpl3Only;
    platforms = lib.platforms.darwin ++ lib.platforms.linux;
    mainProgram = "kitty";
  };
}

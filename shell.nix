with import <nixpkgs> {};

mkShell {
  buildInputs = with pkgs; [
    python3
    python3Packages.pyqt6
    openjdk
    gnumake
    scrcpy
    android-tools
    unzip
    zip
  ];

  shellHook = ''
    echo "Entering scrcpy-app development shell"
    echo "Use: make android-compile, make dex, make deploy"
  '';
}

{
  description = "Scrcpy App Launcher";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs {
      inherit system;
      config.allowUnfree = true;
    };
    androidSdk = (pkgs.androidenv.composeAndroidPackages {
      platformVersions = ["35"];
      buildToolsVersions = ["35.0.0"];
    }).androidsdk;
    pythonEnv = pkgs.python3.withPackages (ps: with ps; [
      pyqt6
    ]);
  in {
    packages.${system} = {
      scrcpy-app = pkgs.stdenv.mkDerivation {
        pname = "scrcpy-app";
        version = "0.1.0";
        src = ./.;

        nativeBuildInputs = [
          pkgs.qt6.wrapQtAppsHook
          pkgs.makeWrapper
        ];
        buildInputs = [
          pkgs.openjdk
          androidSdk
          pythonEnv
          pkgs.qt6.qtbase
          pkgs.scrcpy
          pkgs.android-tools
        ];

        # Run Makefile to produce the jar
        buildPhase = ''
          echo "Running make to build jar"
          mkdir -p build-android
          ${pkgs.openjdk}/bin/javac -cp ${androidSdk}/platforms/android-35/android.jar -d build-android Main.java
          ${pkgs.openjdk}/bin/jar cvf AppIconExtractor.jar -C build-android Main.class
          ${androidSdk}/libexec/android-sdk/build-tools/35.0.0/d8 --output icon-extractor.jar AppIconExtractor.jar
        '';

        installPhase = ''
          mkdir -p $out/bin $out/share/applications
          cp scrcpy-app.py $out/bin/.scrcpy-app-wrapped
          chmod +x $out/bin/.scrcpy-app-wrapped

        makeWrapper ${pythonEnv}/bin/python3 $out/bin/scrcpy-app \
            --add-flags "$out/bin/.scrcpy-app-wrapped" \
            --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.scrcpy pkgs.android-tools ]}

          cp icon-extractor.jar $out/bin/icon-extractor.jar

          mkdir -p $out/share/applications
          cp ${./packaging/scrcpy-app.desktop} $out/share/applications/scrcpy-app.desktop
        '';

        dontWrapQtApps = true;
        preFixup = ''
          wrapQtApp $out/bin/scrcpy-app
        '';

        meta = with pkgs.lib; {
          description = "Scrcpy App Launcher";
          license = licenses.mit;
          maintainers = [ ];
        };
      };
    };

    defaultPackage.${system} = self.packages.${system}.scrcpy-app;
  };
}

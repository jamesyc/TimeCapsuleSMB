class Tcapsule < Formula
  include Language::Python::Virtualenv

  desc "Install Samba 4 on Apple Time Capsules and AirPort Extremes"
  homepage "https://github.com/jamesyc/TimeCapsuleSMB"
  url "https://github.com/jamesyc/TimeCapsuleSMB/archive/refs/tags/v@VERSION@.tar.gz"
  sha256 "@SOURCE_SHA256@"
  license "GPL-3.0-only"

  depends_on arch: :arm64
  depends_on :macos
  depends_on "python@3.15"

  # The Python resources are written for each release by
  # `brew update-python-resources`.

  def install
    virtualenv_install_with_resources
    # The device payloads deploy copies to the Time Capsule.
    (libexec/"distribution").install "bin"
    # Homebrew's own command: its messages say `tcapsule`, and it finds the
    # payloads above. Its settings live in the user's folder, apart from the
    # Mac app's, since every upgrade replaces this versioned Cellar folder.
    # A TCAPSULE_CONFIG or TCAPSULE_STATE_DIR the user sets still wins.
    settings = "$HOME/Library/Application Support/TimeCapsuleSMB/CLI"
    rm bin/"tcapsule"
    (bin/"tcapsule").write_env_script libexec/"bin/tcapsule",
      TCAPSULE_CLIENT:            "homebrew",
      TCAPSULE_DISTRIBUTION_ROOT: libexec/"distribution",
      TCAPSULE_STATE_DIR:         "${TCAPSULE_STATE_DIR:-#{settings}}",
      TCAPSULE_CONFIG:            "${TCAPSULE_CONFIG:-#{settings}/.env}"
  end

  test do
    assert_equal "tcapsule #{version}", shell_output("#{bin}/tcapsule --version").strip
    # The Python packages import, and every payload deploy copies is where the
    # wrapper points with the hash the release lists. An empty token keeps the
    # test out of telemetry.
    ENV["TCAPSULE_TELEMETRY_TOKEN"] = ""
    # The wrapper's TCAPSULE_CLIENT reaches Python: Homebrew needs no bootstrap.
    assert_match "nothing to bootstrap", shell_output("#{bin}/tcapsule bootstrap")
    assert JSON.parse(shell_output("#{bin}/tcapsule validate-install --json"))["ok"]
    # Its settings are in the user's folder, not the Cellar.
    paths = JSON.parse(shell_output("#{bin}/tcapsule paths --json"))
    assert_equal "#{Dir.home}/Library/Application Support/TimeCapsuleSMB/CLI/.env", paths["config_path"]
  end
end

class Tcapsule < Formula
  include Language::Python::Virtualenv

  desc "Install Samba 4 on Apple Time Capsules and AirPort Extremes"
  homepage "https://github.com/jamesyc/TimeCapsuleSMB"
  url "https://github.com/jamesyc/TimeCapsuleSMB/archive/refs/tags/v@VERSION@.tar.gz"
  sha256 "@SOURCE_SHA256@"
  license "GPL-3.0-only"

  depends_on "python@3.15"

  # On Linux, doctor's SMB checks use smbclient; on macOS they use smbutil.
  on_linux do
    depends_on "samba"
  end

  # The Python resources are written for each release by
  # `brew update-python-resources`.

  def install
    virtualenv_install_with_resources
    # The device payloads deploy copies to the Time Capsule.
    (libexec/"distribution").install "bin"
    # Homebrew's own command: its messages say `tcapsule`, and it finds the
    # payloads above. Its settings live in the user's data folder, the one
    # its flash backups use, since every upgrade replaces this versioned
    # Cellar folder; on macOS they sit apart from the Mac app's.
    # A TCAPSULE_CONFIG or TCAPSULE_STATE_DIR the user sets still wins.
    settings = if OS.mac?
      "$HOME/Library/Application Support/TimeCapsuleSMB/CLI"
    else
      "${XDG_DATA_HOME:-$HOME/.local/share}/timecapsulesmb/CLI"
    end
    rm bin/"tcapsule"
    (bin/"tcapsule").write_env_script libexec/"bin/tcapsule",
      TCAPSULE_CLIENT:            "homebrew",
      TCAPSULE_DISTRIBUTION_ROOT: libexec/"distribution",
      TCAPSULE_STATE_DIR:         "${TCAPSULE_STATE_DIR:-#{settings}}",
      TCAPSULE_CONFIG:            "${TCAPSULE_CONFIG:-#{settings}/.env}"
  end

  test do
    assert_equal "tcapsule #{version}", shell_output("#{bin}/tcapsule --version").strip
    # An empty token keeps the test out of telemetry.
    ENV["TCAPSULE_TELEMETRY_TOKEN"] = ""
    # The wrapper's TCAPSULE_CLIENT reaches Python: Homebrew needs no bootstrap.
    assert_match "nothing to bootstrap", shell_output("#{bin}/tcapsule bootstrap")
    assert JSON.parse(shell_output("#{bin}/tcapsule validate-install --json"))["ok"]
    # Its settings are in the user's folder, not the Cellar.
    paths = JSON.parse(shell_output("#{bin}/tcapsule paths --json"))
    settings = if OS.mac?
      "#{Dir.home}/Library/Application Support/TimeCapsuleSMB/CLI"
    else
      "#{ENV.fetch("XDG_DATA_HOME", "").presence || "#{Dir.home}/.local/share"}/timecapsulesmb/CLI"
    end
    assert_equal "#{settings}/.env", paths["config_path"]
  end
end

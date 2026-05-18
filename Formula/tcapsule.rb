class Tcapsule < Formula
  include Language::Python::Virtualenv

  desc "Deploy modern Samba to Apple AirPort Time Capsules"
  homepage "https://github.com/jamesyc/TimeCapsuleSMB"
  url "https://github.com/jamesyc/TimeCapsuleSMB/archive/refs/tags/v2.1.1.tar.gz"
  sha256 "bb475066b58cfbd1ac71e5d035352a808f4acb49b9967a47272191922810cd3e"
  license "GPL-3.0-only"
  head "https://github.com/jamesyc/TimeCapsuleSMB.git", branch: "main"

  depends_on "python-setuptools" => :build
  depends_on "python@3.14"
  depends_on "samba"
  depends_on "sshpass"

  resource "cython" do
    url "https://files.pythonhosted.org/packages/39/e1/c0d92b1258722e1bc62a12e630c33f1f842fdab53fd8cd5de2f75c6449a9/cython-3.2.3.tar.gz"
    sha256 "f13832412d633376ffc08d751cc18ed0d7d00a398a4065e2871db505258748a6"
  end

  resource "flit-core" do
    url "https://files.pythonhosted.org/packages/69/59/b6fc2188dfc7ea4f936cd12b49d707f66a1cb7a1d2c16172963534db741b/flit_core-3.12.0.tar.gz"
    sha256 "18f63100d6f94385c6ed57a72073443e1a71a4acb4339491615d0f16d6ff01b2"
  end

  resource "ifaddr" do
    url "https://files.pythonhosted.org/packages/e8/ac/fb4c578f4a3256561548cd825646680edcadb9440f3f68add95ade1eb791/ifaddr-0.2.0.tar.gz"
    sha256 "cc0cbfcaabf765d44595825fb96a99bb12c79716b73b44330ea38ee2b0c4aed4"
  end

  resource "pexpect" do
    url "https://files.pythonhosted.org/packages/42/92/cc564bf6381ff43ce1f4d06852fc19a2f11d180f23dc32d9588bee2f149d/pexpect-4.9.0.tar.gz"
    sha256 "ee7d41123f3c9911050ea2c2dac107568dc43b2d3b0c7557a33212c398ead30f"
  end

  resource "poetry-core" do
    url "https://files.pythonhosted.org/packages/54/ef/a16c11de95b638341961765e072dfdd4c9a0be51d6b22d594c5f3255e4bb/poetry_core-2.2.1.tar.gz"
    sha256 "97e50d8593c8729d3f49364b428583e044087ee3def1e010c6496db76bd65ac5"
  end

  resource "ptyprocess" do
    url "https://files.pythonhosted.org/packages/20/e5/16ff212c1e452235a90aeb09066144d0c5a6a8c0834397e03f5224495c4e/ptyprocess-0.7.0.tar.gz"
    sha256 "5c5d0a3b48ceee0b48485e0c26037c0acd7d29765ca3fbb5cb3831d347423220"
  end

  resource "pycryptodome" do
    url "https://files.pythonhosted.org/packages/8e/a6/8452177684d5e906854776276ddd34eca30d1b1e15aa1ee9cefc289a33f5/pycryptodome-3.23.0.tar.gz"
    sha256 "447700a657182d60338bab09fdb27518f8856aecd80ae4c6bdddb67ff5da44ef"
  end

  resource "zeroconf" do
    url "https://files.pythonhosted.org/packages/67/46/10db987799629d01930176ae523f70879b63577060d63e05ebf9214aba4b/zeroconf-0.148.0.tar.gz"
    sha256 "03fcca123df3652e23d945112d683d2f605f313637611b7d4adf31056f681702"
  end

  resource "zopfli" do
    url "https://files.pythonhosted.org/packages/5e/7c/a8f6696e694709e2abcbccd27d05ef761e9b6efae217e11d977471555b62/zopfli-0.2.3.post1.tar.gz"
    sha256 "96484dc0f48be1c5d7ae9f38ed1ce41e3675fd506b27c11a6607f14b49101e99"
  end

  def install
    pkgshare.install "bin"

    venv = virtualenv_create(libexec, "python3.14")
    venv.pip_install resources, build_isolation: false
    venv.pip_install buildpath, build_isolation: false
    (bin/"tcapsule").write_env_script libexec/"bin/tcapsule", TCAPSULE_DISTRIBUTION_ROOT: pkgshare
  end

  test do
    ENV["TCAPSULE_SKIP_VERSION_CHECK"] = "1"

    validation = shell_output("#{bin}/tcapsule validate-install --json")
    assert_match '"ok": true', validation

    paths = shell_output("#{bin}/tcapsule paths --json")
    assert_match pkgshare.to_s, paths
    assert_match "artifact-manifest.json", paths
  end
end

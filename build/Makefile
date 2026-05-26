# NetBSD VM build orchestration.
#
# This Makefile is intentionally separate from the repository-root Makefile:
# the root Makefile is for macOS development tasks, while this file is meant
# to run from the NetBSD build VM after the repository has been synced there.
#
# Keep the shell scripts as the source of truth for compiler flags, source
# patching, and per-lane environment setup. Make only names the lanes and
# preserves the required sequencing. The SDK download/bootstrap targets are
# deliberately explicit and are not dependencies of artifact builds; rebuilding
# those toolchains is expensive and should only happen intentionally.

.PHONY: help
.PHONY: require-build-tree preflight
.PHONY: mdns mdns-netbsd7 mdns-netbsd4le mdns-netbsd4be mdns-all
.PHONY: nbns nbns-netbsd7 nbns-netbsd4le nbns-netbsd4be nbns-all
.PHONY: advertisers advertisers-netbsd7 advertisers-netbsd4le advertisers-netbsd4be advertisers-all
.PHONY: samba4 samba4-netbsd7 samba4-netbsd4le samba4-netbsd4be samba4-all
.PHONY: samba4x samba4x-netbsd7 samba4x-netbsd4le samba4x-netbsd4be samba4x-all
.PHONY: hello hello-netbsd7 hello-netbsd4le hello-netbsd4be hello-all
.PHONY: sdk-download-netbsd7 sdk-download-netbsd4le sdk-download-netbsd4be
.PHONY: sdk-bootstrap-netbsd7 sdk-bootstrap-netbsd4le sdk-bootstrap-netbsd4be
.PHONY: sdk-bootstrap-tools-netbsd7 sdk-bootstrap-tools-netbsd4le sdk-bootstrap-tools-netbsd4be
.PHONY: sdk-bootstrap-distribution-netbsd7 sdk-bootstrap-distribution-netbsd4le sdk-bootstrap-distribution-netbsd4be
.PHONY: samba4x-cross-answers samba4x-cross-answers-netbsd7 samba4x-cross-answers-netbsd4le samba4x-cross-answers-netbsd4be

help:
	@printf '%s\n' 'Run this from the NetBSD VM as root: cd build && make <target>'
	@printf '%s\n' 'Optional: copy .env.example to .env only when overriding default paths or target device settings.'
	@printf '%s\n' ''
	@printf '%s\n' 'Common artifact targets:'
	@printf '%s\n' '  make advertisers-all       # mdns + nbns for NetBSD 6/7, NetBSD 4 LE, NetBSD 4 BE'
	@printf '%s\n' '  make mdns-all              # mdns for all three lanes'
	@printf '%s\n' '  make nbns-all              # nbns for all three lanes'
	@printf '%s\n' '  make samba4-netbsd7        # download Samba 4 source, then build NetBSD 6/7 lane'
	@printf '%s\n' '  make samba4-netbsd4le      # download Samba 4 source, then build NetBSD 4 LE lane'
	@printf '%s\n' '  make samba4-netbsd4be      # download Samba 4 source, then build NetBSD 4 BE lane'
	@printf '%s\n' ''
	@printf '%s\n' 'Toolchain targets are explicit only:'
	@printf '%s\n' '  make sdk-download-netbsd7'
	@printf '%s\n' '  make sdk-bootstrap-netbsd7'
	@printf '%s\n' '  make sdk-download-netbsd4le'
	@printf '%s\n' '  make sdk-bootstrap-netbsd4le'
	@printf '%s\n' '  make sdk-download-netbsd4be'
	@printf '%s\n' '  make sdk-bootstrap-netbsd4be'

require-build-tree:
	@missing=0; \
	for path in \
		env.sh \
		_patch_helpers.sh \
		mdns-advertiser.c \
		nbns-advertiser.c \
		samba4-cross-exec.sh \
		patches/samba4x/series \
		patches/gnutls/series \
		cross-answers/samba4x-4.24.1-netbsd7.answers \
		cross-answers/samba4x-4.24.1-netbsd4le.answers \
		cross-answers/samba4x-4.24.1-netbsd4be.answers; do \
		if [ ! -e "$$path" ]; then \
			echo "Missing copied build support file: $$path"; \
			missing=1; \
		fi; \
	done; \
	if [ "$$missing" -ne 0 ]; then \
		echo 'Copy the full build/ directory, including patches/ and cross-answers/.'; \
		exit 1; \
	fi

preflight: require-build-tree
	@printf '%s\n' 'build/ preflight passed.'

mdns: require-build-tree mdns-netbsd7

mdns-netbsd7: require-build-tree
	./mdns.sh

mdns-netbsd4le: require-build-tree
	./mdnsoldle.sh

mdns-netbsd4be: require-build-tree
	./mdnsoldbe.sh

mdns-all: require-build-tree
	./mdns.sh
	./mdnsoldle.sh
	./mdnsoldbe.sh

nbns: require-build-tree nbns-netbsd7

nbns-netbsd7: require-build-tree
	./nbns.sh

nbns-netbsd4le: require-build-tree
	./nbnsoldle.sh

nbns-netbsd4be: require-build-tree
	./nbnsoldbe.sh

nbns-all: require-build-tree
	./nbns.sh
	./nbnsoldle.sh
	./nbnsoldbe.sh

advertisers: require-build-tree advertisers-netbsd7

advertisers-netbsd7: require-build-tree
	./mdns.sh
	./nbns.sh

advertisers-netbsd4le: require-build-tree
	./mdnsoldle.sh
	./nbnsoldle.sh

advertisers-netbsd4be: require-build-tree
	./mdnsoldbe.sh
	./nbnsoldbe.sh

advertisers-all: require-build-tree
	./mdns.sh
	./mdnsoldle.sh
	./mdnsoldbe.sh
	./nbns.sh
	./nbnsoldle.sh
	./nbnsoldbe.sh

samba4: require-build-tree samba4-netbsd7

samba4-netbsd7: require-build-tree
	./downloadsamba4.sh
	./samba4.sh

samba4-netbsd4le: require-build-tree
	./downloadsamba4oldle.sh
	./samba4oldle.sh

samba4-netbsd4be: require-build-tree
	./downloadsamba4oldbe.sh
	./samba4oldbe.sh

samba4-all: require-build-tree
	./downloadsamba4.sh
	./samba4.sh
	./downloadsamba4oldle.sh
	./samba4oldle.sh
	./downloadsamba4oldbe.sh
	./samba4oldbe.sh

samba4x: require-build-tree samba4x-netbsd7

samba4x-netbsd7: require-build-tree
	./downloadsamba4x.sh
	./samba4x.sh

samba4x-netbsd4le: require-build-tree
	./downloadsamba4xoldle.sh
	./samba4xoldle.sh

samba4x-netbsd4be: require-build-tree
	./downloadsamba4xoldbe.sh
	./samba4xoldbe.sh

samba4x-all: require-build-tree
	./samba4xall.sh

hello: require-build-tree hello-netbsd7

hello-netbsd7: require-build-tree
	./hello.sh

hello-netbsd4le: require-build-tree
	./hellooldle.sh

hello-netbsd4be: require-build-tree
	./hellooldbe.sh

hello-all: require-build-tree
	./hello.sh
	./hellooldle.sh
	./hellooldbe.sh

sdk-download-netbsd7: require-build-tree
	./download.sh

sdk-download-netbsd4le: require-build-tree
	./downloadoldle.sh

sdk-download-netbsd4be: require-build-tree
	./downloadoldbe.sh

sdk-bootstrap-netbsd7: require-build-tree
	./bootstrap.sh

sdk-bootstrap-netbsd4le: require-build-tree
	./bootstrapoldle.sh

sdk-bootstrap-netbsd4be: require-build-tree
	./bootstrapoldbe.sh

sdk-bootstrap-tools-netbsd7: require-build-tree
	./bootstrap.sh tools

sdk-bootstrap-tools-netbsd4le: require-build-tree
	./bootstrapoldle.sh tools

sdk-bootstrap-tools-netbsd4be: require-build-tree
	./bootstrapoldbe.sh tools

sdk-bootstrap-distribution-netbsd7: require-build-tree
	./bootstrap.sh distribution

sdk-bootstrap-distribution-netbsd4le: require-build-tree
	./bootstrapoldle.sh distribution

sdk-bootstrap-distribution-netbsd4be: require-build-tree
	./bootstrapoldbe.sh distribution

samba4x-cross-answers: require-build-tree samba4x-cross-answers-netbsd7

samba4x-cross-answers-netbsd7: require-build-tree
	./generate-samba4x-cross-answers.sh

samba4x-cross-answers-netbsd4le: require-build-tree
	./generate-samba4x-cross-answers-oldle.sh

samba4x-cross-answers-netbsd4be: require-build-tree
	./generate-samba4x-cross-answers-oldbe.sh

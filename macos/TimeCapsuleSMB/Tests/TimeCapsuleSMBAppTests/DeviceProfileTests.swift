import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceProfileTests: XCTestCase {
    func testStableConfigPathFromProfileID() {
        let appSupport = URL(fileURLWithPath: "/tmp/TimeCapsuleSMBTests", isDirectory: true)

        let configURL = DeviceProfile.configURL(for: "profile-1", applicationSupportURL: appSupport)

        XCTAssertEqual(configURL.path, "/tmp/TimeCapsuleSMBTests/Devices/profile-1/.env")
    }

    func testDisplayNameFallbackOrder() {
        var profile = makeProfile(displayName: "  ", host: "10.0.0.2", bonjourName: "Office Capsule", model: "Model")
        XCTAssertEqual(profile.title, "Office Capsule")

        profile.bonjourName = "  "
        XCTAssertEqual(profile.title, "Model")

        profile.model = nil
        XCTAssertEqual(profile.title, "10.0.0.2")

        profile.host = "  "
        XCTAssertEqual(profile.title, "Time Capsule")
    }

    func testNetworkIdentityKeepsMultipleAddressesAndPrefersRegularIPv4() {
        let profile = makeProfile(
            host: "root@10.0.0.2",
            hostname: "office-capsule.local.",
            addresses: ["169.254.44.9", "10.0.0.2", "fd00::2"]
        )

        XCTAssertEqual(profile.addresses, ["169.254.44.9", "10.0.0.2", "fd00::2"])
        XCTAssertEqual(profile.connectionTarget, "10.0.0.2")
        XCTAssertEqual(profile.displayTarget, "office-capsule.local")
        XCTAssertEqual(profile.addressSummary, "IPv4 10.0.0.2  IPv6 fd00::2")
    }

    func testNetworkIdentitySupportsIPv6OnlyProfiles() {
        let profile = makeProfile(
            host: "root@fd00::2",
            bonjourName: nil,
            hostname: nil,
            addresses: ["fd00::2"]
        )

        XCTAssertEqual(profile.connectionTarget, "fd00::2")
        XCTAssertEqual(profile.displayTarget, "fd00::2")
        XCTAssertEqual(profile.addressSummary, "IPv6 fd00::2")
    }

    func testDuplicateMatchingUsesBonjourHostHostnameAndAddressIdentityButNotWeakMetadata() {
        let first = makeProfile(
            id: "one",
            host: "  TCAPSULE.LOCAL.  ",
            bonjourFullname: "Office Capsule._airport._tcp.local.",
            hostname: "office-capsule.local.",
            addresses: ["10.0.0.2", "169.254.44.9"],
            syap: "119",
            model: "Time Capsule"
        )
        let sameFullname = makeProfile(
            id: "two",
            host: "10.0.0.9",
            bonjourFullname: " office capsule._AIRPORT._tcp.local. "
        )
        let sameHost = makeProfile(id: "three", host: "tcapsule.local.")
        let sameHostWithRootUser = makeProfile(id: "five", host: "root@tcapsule.local")
        let sameHostname = makeProfile(id: "six", host: "10.0.0.10", hostname: "office-capsule.local.")
        let sameAddress = makeProfile(id: "seven", host: "10.0.0.11", addresses: ["10.0.0.2"])
        let sameLinkLocalAddress = makeProfile(id: "eight", host: "10.0.0.13", addresses: ["169.254.44.9"])
        let weakMetadataOnly = makeProfile(id: "four", host: "10.0.0.12", syap: "119", model: "Time Capsule")

        XCTAssertTrue(DeviceProfile.matches(first, sameFullname))
        XCTAssertTrue(DeviceProfile.matches(first, sameHost))
        XCTAssertTrue(DeviceProfile.matches(first, sameHostWithRootUser))
        XCTAssertTrue(DeviceProfile.matches(first, sameHostname))
        XCTAssertTrue(DeviceProfile.matches(first, sameAddress))
        XCTAssertFalse(DeviceProfile.matches(first, sameLinkLocalAddress))
        XCTAssertFalse(DeviceProfile.matches(first, weakMetadataOnly))
    }

    func testRuntimeContextUsesProfileConfigPath() {
        let profile = makeProfile(id: "abc", host: "10.0.0.2", configPath: "/tmp/devices/abc/.env")

        XCTAssertEqual(profile.runtimeContext.profileID, "abc")
        XCTAssertEqual(profile.runtimeContext.configURL.path, "/tmp/devices/abc/.env")
    }

    func testProfileSettingsDecodeMissingNewKeysWithDefaults() throws {
        let data = Data("""
        {
          "nbnsEnabled": false,
          "debugLogging": true,
          "mountWaitSeconds": 45
        }
        """.utf8)

        let settings = try JSONDecoder().decode(DeviceProfileSettings.self, from: data)

        XCTAssertEqual(settings.nbnsEnabled, false)
        XCTAssertEqual(settings.internalShareUseDiskRoot, false)
        XCTAssertEqual(settings.smbBrowseCompatibility, false)
        XCTAssertEqual(settings.mdnsAdvertiseAFP, false)
        XCTAssertEqual(settings.anyProtocol, false)
        XCTAssertEqual(settings.fruitMetadataNetatalk, true)
        XCTAssertEqual(settings.debugLogging, true)
        XCTAssertEqual(settings.mountWaitSeconds, 45)
        XCTAssertEqual(settings.ataIdleSeconds, 300)
        XCTAssertNil(settings.ataStandby)
    }

    func testProfileSettingsDecodeSmbBrowseCompatibility() throws {
        let data = Data("""
        {
          "nbnsEnabled": true,
          "smbBrowseCompatibility": true,
          "mdnsAdvertiseAFP": true,
          "fruitMetadataNetatalk": true,
          "debugLogging": false,
          "mountWaitSeconds": 45
        }
        """.utf8)

        let settings = try JSONDecoder().decode(DeviceProfileSettings.self, from: data)

        XCTAssertEqual(settings.smbBrowseCompatibility, true)
        XCTAssertEqual(settings.mdnsAdvertiseAFP, true)
        XCTAssertEqual(settings.fruitMetadataNetatalk, true)
    }

    func testProfileSettingsDecodeLegacyStringAtaValues() throws {
        let data = Data("""
        {
          "nbnsEnabled": true,
          "debugLogging": false,
          "mountWaitSeconds": 45,
          "ataIdleSeconds": "0",
          "ataStandby": "120"
        }
        """.utf8)

        let settings = try JSONDecoder().decode(DeviceProfileSettings.self, from: data)

        XCTAssertEqual(settings.ataIdleSeconds, 0)
        XCTAssertEqual(settings.ataStandby, 120)
    }

    func testProfileSettingsInvalidLegacyAtaValuesFallbackSafely() throws {
        let data = Data("""
        {
          "nbnsEnabled": true,
          "debugLogging": false,
          "mountWaitSeconds": 45,
          "ataIdleSeconds": "bad",
          "ataStandby": "bad"
        }
        """.utf8)

        let settings = try JSONDecoder().decode(DeviceProfileSettings.self, from: data)

        XCTAssertEqual(settings.ataIdleSeconds, 300)
        XCTAssertNil(settings.ataStandby)
    }

    func testTraitsClassifyNetBSD4NetBSD6AndUnsupportedDevices() {
        let netbsd4 = makeProfile(payloadFamily: "netbsd4_samba4")
        XCTAssertTrue(netbsd4.traits.isNetBSD4)
        XCTAssertFalse(netbsd4.traits.isNetBSD6)
        XCTAssertTrue(netbsd4.traits.needsActivationAfterReboot)
        XCTAssertTrue(netbsd4.traits.supportsFlashBootHook)
        XCTAssertTrue(netbsd4.traits.isSupported)

        let netbsd4ByRelease = makeProfile(osRelease: "4.0")
        XCTAssertTrue(netbsd4ByRelease.traits.isNetBSD4)
        XCTAssertTrue(netbsd4ByRelease.traits.supportsFlashBootHook)

        let netbsd6 = makeProfile(osRelease: "6.0")
        XCTAssertFalse(netbsd6.traits.isNetBSD4)
        XCTAssertTrue(netbsd6.traits.isNetBSD6)
        XCTAssertFalse(netbsd6.traits.needsActivationAfterReboot)
        XCTAssertFalse(netbsd6.traits.supportsFlashBootHook)
        XCTAssertTrue(netbsd6.traits.isSupported)

        let unsupported = makeProfile(payloadFamily: "unsupported", deviceGeneration: "unsupported")
        XCTAssertFalse(unsupported.traits.isSupported)
    }

    private func makeProfile(
        id: String = "profile",
        displayName: String = "Office Capsule",
        host: String = "10.0.0.2",
        bonjourName: String? = nil,
        bonjourFullname: String? = nil,
        hostname: String? = nil,
        addresses: [String] = [],
        syap: String? = nil,
        model: String? = nil,
        osRelease: String? = nil,
        payloadFamily: String? = nil,
        deviceGeneration: String? = nil,
        configPath: String = "/tmp/profile/.env"
    ) -> DeviceProfile {
        DeviceProfile(
            id: id,
            displayName: displayName,
            host: host,
            bonjourName: bonjourName,
            bonjourFullname: bonjourFullname,
            hostname: hostname,
            addresses: addresses,
            syap: syap,
            model: model,
            osName: nil,
            osRelease: osRelease,
            arch: nil,
            elfEndianness: nil,
            payloadFamily: payloadFamily,
            deviceGeneration: deviceGeneration,
            configPath: configPath,
            keychainAccount: id,
            createdAt: Date(timeIntervalSince1970: 10),
            updatedAt: Date(timeIntervalSince1970: 20),
            lastCheckup: nil,
            lastDeployState: nil,
            settings: .default,
            passwordState: .unknown
        )
    }
}

import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class BonjourServiceLabelTests: XCTestCase {
    private let labels = [
        "AirPort Time\u{00A0}Capsule ", " Capsule ", "\u{00A0}Capsule\u{00A0}",
        "\tCapsule\t", "   ", "Capsule\u{0085}Office", "Capsule\u{2028}Office",
        "Capsule\u{2029}Office", "Café 胶囊 🛜", "Cafe\u{0301}", "Capsule.",
        "...STARTING...", "Bob's.Café\\123"
    ]

    func testIdentityAndProfileCodingPreserveRawLabels() throws {
        for label in labels {
            let original = profile(name: label, fullname: "\(label)._airport._tcp.local.")
            XCTAssertEqual(original.network.bonjourName, label)
            XCTAssertEqual(original.network.bonjourFullname, "\(label)._airport._tcp.local.")
            let decoded = try JSONDecoder().decode(DeviceProfile.self, from: JSONEncoder().encode(original))
            XCTAssertEqual(decoded.network, original.network)
            XCTAssertEqual(decoded.bonjourName, label)
            XCTAssertEqual(Array(try XCTUnwrap(decoded.bonjourName).utf8), Array(label.utf8))
            XCTAssertEqual(decoded.bonjourFullname, original.bonjourFullname)
        }
    }

    func testEmptyIdentityFieldsRemainAbsentWhileWhitespaceLabelsRemainPresent() {
        let empty = DeviceNetworkIdentity(configuredSSHTarget: "root@10.0.0.2", hostname: "  host.local  ",
            bonjourName: "", bonjourFullname: "")
        XCTAssertNil(empty.bonjourName)
        XCTAssertNil(empty.bonjourFullname)
        XCTAssertEqual(empty.hostname, "host.local")
        let spaces = DeviceNetworkIdentity(configuredSSHTarget: "root@10.0.0.2", bonjourName: "   ")
        XCTAssertEqual(spaces.bonjourName, "   ")
    }

    func testDisplayNameFormattingDoesNotAlterNetworkIdentity() {
        var original = profile(name: " Capsule ", fullname: " Capsule ._airport._tcp.local.")
        XCTAssertEqual(original.title, "Capsule")
        original.displayName = "User's display name"
        XCTAssertEqual(original.title, "User's display name")
        XCTAssertEqual(original.bonjourName, " Capsule ")
        XCTAssertEqual(original.bonjourFullname, " Capsule ._airport._tcp.local.")
    }

    func testSMBAddressesPreserveLabelsFromEveryRelatedServiceAndNameFallback() {
        for label in labels {
            // Only a label's dots and backslashes take DNS escapes.
            let host = label.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: ".", with: "\\.") + "._smb._tcp.local"
            for service in ["_airport", "_adisk", "_device-info", "_smb"] {
                for rootDot in ["", "."] {
                    let original = profile(name: "different hint", fullname: "\(label).\(service)._tcp.local\(rootDot)")
                    XCTAssertEqual(SMBAddressPolicy.preferredHost(for: original), host)
                    XCTAssertEqual(SMBAddressPolicy.url(for: original)?.absoluteString.removingPercentEncoding,
                                   "smb://\(host)")
                }
            }
            let fallback = profile(name: label, fullname: nil)
            XCTAssertEqual(SMBAddressPolicy.preferredHost(for: fallback), host)
            XCTAssertEqual(SMBAddressPolicy.url(for: fallback, account: "James Chang")?.absoluteString.removingPercentEncoding,
                           "smb://James Chang@\(host)")
        }
    }

    func testSMBURLsUseTheFormsMacOSConnectedTo() {
        // smbutil view against NetBSD 4 LE (2026-10-03): the raw dot and %2E
        // forms gave "No route to host"; these forms reached the server.
        let verified = [
            "Time.Capsule": "smb://Time%5C.Capsule._smb._tcp.local",
            "Time\\Capsule": "smb://Time%5C%5CCapsule._smb._tcp.local",
            "   ": "smb://%20%20%20._smb._tcp.local",
            "AirPort Time Capsule": "smb://AirPort%20Time%20Capsule._smb._tcp.local"
        ]
        for (label, url) in verified {
            XCTAssertEqual(SMBAddressPolicy.url(for: profile(name: label, fullname: "\(label)._airport._tcp.local."))?.absoluteString, url)
            XCTAssertEqual(SMBAddressPolicy.url(for: profile(name: label, fullname: nil))?.absoluteString, url)
        }
    }

    func testDiscoveredDeviceDisplayNameTrimsOnlyForDisplay() {
        let cases: [(name: String, hostname: String, display: String)] = [
            ("   ", "Base-Station-619b7d.local", "Base-Station-619b7d.local"),
            (" AirPort Time\u{00A0}Capsule ", "host.local", "AirPort Time\u{00A0}Capsule"),
            ("\u{00A0}\t", "", "AirPort Device"),
            ("Time.Capsule", "Time-Capsule.local", "Time.Capsule")
        ]
        for item in cases {
            let device = DiscoveredDevice(id: "observed", name: item.name, connectionTarget: "10.0.0.2",
                sshHost: nil, hostname: item.hostname, networkAddresses: [], syap: nil, model: nil, rawRecord: .object([:]))
            XCTAssertEqual(device.displayName, item.display)
            XCTAssertEqual(device.name, item.name)
            XCTAssertEqual(device.observedIdentity.bonjourName, item.name)
        }
    }

    func testProfileFromAWhitespaceOnlyLabelGetsAVisibleName() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let device = DiscoveredDevice(id: "observed", name: "   ", connectionTarget: "10.0.0.2",
            sshHost: "root@10.0.0.2", hostname: "Base-Station-619b7d.local", networkAddresses: [], syap: "116",
            model: "TimeCapsule6,116", rawRecord: .object(["fullname": .string("   ._airport._tcp.local.")]))
        let saved = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: device, passwordState: .available, preferredID: "blank-profile")
        XCTAssertEqual(saved.displayName, "Base-Station-619b7d.local")
        XCTAssertEqual(saved.title, "Base-Station-619b7d.local")
        XCTAssertEqual(saved.bonjourName, "   ")
        XCTAssertEqual(SMBAddressPolicy.url(for: saved)?.absoluteString, "smb://%20%20%20._smb._tcp.local")
    }

    func testWhitespaceVariantsDoNotShareAServiceIdentity() {
        let plain = DeviceNetworkIdentity(configuredSSHTarget: "10.0.0.2", bonjourFullname: "Capsule._airport._tcp.local.")
        for label in [" Capsule", "Capsule ", "\u{00A0}Capsule"] {
            let other = DeviceNetworkIdentity(configuredSSHTarget: "10.0.0.9", bonjourFullname: "\(label)._airport._tcp.local.")
            XCTAssertFalse(plain.sharesName(with: other))
            XCTAssertFalse(plain.matches(other))
        }
    }

    func testRegistrySaveReloadAndDisplayEditPreserveTheDiscoveredLabel() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let label = " AirPort Time\u{00A0}Capsule "
        let fullname = "\(label)._airport._tcp.local."
        let device = DiscoveredDevice(id: "observed", name: label, connectionTarget: "10.0.0.2",
            sshHost: "root@10.0.0.2", hostname: "host.local", networkAddresses: [], syap: "116",
            model: "TimeCapsule6,116", rawRecord: .object(["fullname": .string(fullname)]))
        let saved = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: device, passwordState: .available, preferredID: "label-profile")
        XCTAssertEqual(saved.bonjourName, label)
        XCTAssertEqual(saved.bonjourFullname, fullname)
        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url)
        await reloaded.load()
        var loaded = try XCTUnwrap(reloaded.profile(id: saved.id))
        XCTAssertEqual(loaded.bonjourName, label)
        XCTAssertEqual(loaded.bonjourFullname, fullname)
        loaded.displayName = "Edited display name"
        _ = try await reloaded.updateProfile(loaded)
        await reloaded.load()
        XCTAssertEqual(reloaded.profile(id: saved.id)?.bonjourName, label)
        XCTAssertEqual(reloaded.profile(id: saved.id)?.bonjourFullname, fullname)
    }

    private func profile(name: String?, fullname: String?) -> DeviceProfile {
        DeviceProfile(id: "label", displayName: "", host: "root@10.0.0.2", bonjourName: name,
            bonjourFullname: fullname, hostname: "host.local", addresses: [], syap: nil,
            model: nil, osName: nil, osRelease: nil, arch: nil, elfEndianness: nil,
            payloadFamily: nil, deviceGeneration: nil, configPath: "/tmp/label/.env",
            keychainAccount: "label", createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2), lastCheckup: nil, lastDeployState: nil,
            settings: .default, passwordState: .available)
    }
}

import XCTest
@testable import TimeCapsuleSMBApp

final class SMBAddressPolicyTests: XCTestCase {
    func testPrefersBonjourSMBServiceOverResolvedHostname() {
        let profile = makeProfile(
            host: "root@10.0.0.2",
            bonjourName: "AirPort Time Capsule",
            bonjourFullname: "AirPort Time Capsule._airport._tcp.local.",
            hostname: "AirPort-Time-Capsule.local."
        )

        XCTAssertEqual(SMBAddressPolicy.preferredHost(for: profile), "AirPort Time Capsule._smb._tcp.local")
        XCTAssertEqual(
            SMBAddressPolicy.url(for: profile, account: "James Chang")?.absoluteString,
            "smb://James%20Chang@AirPort%20Time%20Capsule._smb._tcp.local"
        )
    }

    func testFallsBackToConfiguredHostWhenBonjourHostnameIsMissing() {
        let profile = makeProfile(host: "root@10.0.0.2", bonjourName: nil, bonjourFullname: nil, hostname: nil)

        XCTAssertEqual(SMBAddressPolicy.preferredHost(for: profile), "10.0.0.2")
        XCTAssertEqual(SMBAddressPolicy.url(for: profile)?.absoluteString, "smb://10.0.0.2")
    }

    func testTrimsURLPathAndTrailingDotFromHostCandidates() {
        let profile = makeProfile(
            host: "smb://office-capsule.local./Data",
            bonjourName: nil,
            bonjourFullname: nil,
            hostname: "  "
        )

        XCTAssertEqual(SMBAddressPolicy.preferredHost(for: profile), "office-capsule.local")
        XCTAssertEqual(SMBAddressPolicy.url(for: profile)?.absoluteString, "smb://office-capsule.local")
    }

    func testCredentialServerCandidatesUseResolvedHostNotBonjourServiceName() {
        let profile = makeProfile(
            host: "root@10.0.0.2",
            bonjourName: "AirPort Time Capsule",
            bonjourFullname: "AirPort Time Capsule._airport._tcp.local.",
            hostname: "AirPort-Time-Capsule.local."
        )

        XCTAssertEqual(SMBAddressPolicy.credentialServerCandidates(for: profile), [
            "AirPort-Time-Capsule.local",
            "10.0.0.2"
        ])
    }

    func testReturnsNilWhenNoUsableHostExists() {
        let profile = makeProfile(host: "  ", bonjourName: nil, bonjourFullname: nil, hostname: ".")

        XCTAssertNil(SMBAddressPolicy.preferredHost(for: profile))
        XCTAssertNil(SMBAddressPolicy.url(for: profile))
    }

    private func makeProfile(
        host: String,
        bonjourName: String? = "Office Capsule",
        bonjourFullname: String? = "Office Capsule._airport._tcp.local.",
        hostname: String?
    ) -> DeviceProfile {
        DeviceProfile(
            id: "device-one",
            displayName: "Office Capsule",
            host: host,
            bonjourName: bonjourName,
            bonjourFullname: bonjourFullname,
            hostname: hostname,
            addresses: [],
            syap: "119",
            model: "TimeCapsule6,116",
            osName: nil,
            osRelease: nil,
            arch: nil,
            elfEndianness: nil,
            payloadFamily: nil,
            deviceGeneration: nil,
            configPath: "/tmp/device-one/.env",
            keychainAccount: "device-one",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastCheckup: nil,
            lastDeploy: nil,
            settings: .default,
            passwordState: .available
        )
    }
}

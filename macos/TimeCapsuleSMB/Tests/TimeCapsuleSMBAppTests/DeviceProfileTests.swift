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

    func testDuplicateMatchingUsesBonjourFullnameAndNormalizedHostOnly() {
        let first = makeProfile(
            id: "one",
            host: "  TCAPSULE.LOCAL.  ",
            bonjourFullname: "Office Capsule._airport._tcp.local.",
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
        let weakMetadataOnly = makeProfile(id: "four", host: "10.0.0.10", syap: "119", model: "Time Capsule")

        XCTAssertTrue(DeviceProfile.matches(first, sameFullname))
        XCTAssertTrue(DeviceProfile.matches(first, sameHost))
        XCTAssertTrue(DeviceProfile.matches(first, sameHostWithRootUser))
        XCTAssertFalse(DeviceProfile.matches(first, weakMetadataOnly))
    }

    func testRuntimeContextUsesProfileConfigPath() {
        let profile = makeProfile(id: "abc", host: "10.0.0.2", configPath: "/tmp/devices/abc/.env")

        XCTAssertEqual(profile.runtimeContext.profileID, "abc")
        XCTAssertEqual(profile.runtimeContext.configURL.path, "/tmp/devices/abc/.env")
    }

    private func makeProfile(
        id: String = "profile",
        displayName: String = "Office Capsule",
        host: String = "10.0.0.2",
        bonjourName: String? = nil,
        bonjourFullname: String? = nil,
        syap: String? = nil,
        model: String? = nil,
        configPath: String = "/tmp/profile/.env"
    ) -> DeviceProfile {
        DeviceProfile(
            id: id,
            displayName: displayName,
            host: host,
            bonjourName: bonjourName,
            bonjourFullname: bonjourFullname,
            hostname: nil,
            addresses: [],
            syap: syap,
            model: model,
            osName: nil,
            osRelease: nil,
            arch: nil,
            elfEndianness: nil,
            payloadFamily: nil,
            deviceGeneration: nil,
            configPath: configPath,
            keychainAccount: id,
            createdAt: Date(timeIntervalSince1970: 10),
            updatedAt: Date(timeIntervalSince1970: 20),
            lastCheckup: nil,
            lastDeploy: nil,
            settings: .default,
            passwordState: .unknown
        )
    }
}

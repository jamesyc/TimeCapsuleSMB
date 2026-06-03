import XCTest
@testable import TimeCapsuleSMBApp

final class HostCompatibilityPolicyTests: XCTestCase {
    func testWarnsForKnownProblemVersions() {
        XCTAssertNotNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 5)))
        XCTAssertNotNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 6)))
        XCTAssertNotNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 7)))
        XCTAssertNotNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 26, minorVersion: 4, patchVersion: 0)))
        XCTAssertNotNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 26, minorVersion: 4, patchVersion: 12)))
    }

    func testDoesNotWarnForAdjacentVersions() {
        XCTAssertNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 4)))
        XCTAssertNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 8)))
        XCTAssertNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 15, minorVersion: 6, patchVersion: 7)))
        XCTAssertNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 26, minorVersion: 3, patchVersion: 9)))
        XCTAssertNil(HostCompatibilityPolicy.warning(for: OperatingSystemVersion(majorVersion: 26, minorVersion: 5, patchVersion: 0)))
    }

    func testDisabledPolicyStillSuppressesWarnings() {
        XCTAssertNil(HostCompatibilityPolicy.warning(
            enabled: false,
            for: OperatingSystemVersion(majorVersion: 15, minorVersion: 7, patchVersion: 5)
        ))
    }
}

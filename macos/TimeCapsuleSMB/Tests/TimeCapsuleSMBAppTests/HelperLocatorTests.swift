import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class HelperLocatorTests: XCTestCase {
    func testLocatorUsesExplicitHelperAndSetsAppEnvironment() throws {
        let temp = try TemporaryDirectory()
        let helper = temp.url.appendingPathComponent("tcapsule")
        try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)

        let locator = HelperLocator(
            environment: [:],
            currentDirectory: temp.url,
            bundle: .main,
            fileManager: .default
        )

        let resolution = try locator.resolve(helperPath: helper.path)
        let environment = locator.helperEnvironment(for: resolution)

        XCTAssertEqual(resolution.executableURL.path, helper.path)
        XCTAssertNotNil(environment["TCAPSULE_CONFIG"])
        XCTAssertNotNil(environment["TCAPSULE_STATE_DIR"])
    }

    func testLocatorDiscoversRepoHelperFromSourceRoot() throws {
        let temp = try TemporaryDirectory()
        let repo = temp.url.appendingPathComponent("Repo", isDirectory: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent(".venv/bin", isDirectory: true), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent("bin", isDirectory: true), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent("src/timecapsulesmb", isDirectory: true), withIntermediateDirectories: true)
        try "".write(to: repo.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let helper = repo.appendingPathComponent(".venv/bin/tcapsule")
        try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)

        let locator = HelperLocator(
            environment: ["TCAPSULE_SOURCE_ROOT": repo.path],
            currentDirectory: temp.url,
            bundle: .main,
            fileManager: .default
        )

        let resolution = try locator.resolve(helperPath: nil)
        let environment = locator.helperEnvironment(for: resolution)

        XCTAssertEqual(resolution.executableURL.path, helper.path)
        XCTAssertEqual(resolution.distributionRootURL?.path, repo.path)
        XCTAssertEqual(environment["TCAPSULE_DISTRIBUTION_ROOT"], repo.path)
    }

    func testLocatorReportsAttemptedPathsWhenMissing() throws {
        let temp = try TemporaryDirectory()
        let locator = HelperLocator(
            environment: ["TCAPSULE_SOURCE_ROOT": temp.url.path],
            currentDirectory: temp.url,
            bundle: .main,
            fileManager: .default
        )

        XCTAssertThrowsError(try locator.resolve(helperPath: nil)) { error in
            guard case HelperLocatorError.notFound(let attempts) = error else {
                return XCTFail("unexpected error \(error)")
            }
            XCTAssertFalse(attempts.isEmpty)
        }
    }
}

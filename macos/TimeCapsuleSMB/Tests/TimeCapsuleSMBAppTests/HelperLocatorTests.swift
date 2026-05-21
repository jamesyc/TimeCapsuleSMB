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
        XCTAssertEqual(resolution.mode, .explicit)
        XCTAssertNil(resolution.toolsBinURL)
        XCTAssertNotNil(environment["TCAPSULE_CONFIG"])
        XCTAssertNotNil(environment["TCAPSULE_STATE_DIR"])
        XCTAssertEqual(environment["PYTHONNOUSERSITE"], "1")
    }

    func testLocatorUsesDeviceContextConfigWithoutChangingAppStateDirectory() throws {
        let temp = try TemporaryDirectory()
        let helper = temp.url.appendingPathComponent("tcapsule")
        try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)
        let context = DeviceRuntimeContext(
            profileID: "device-one",
            configURL: temp.url.appendingPathComponent("Devices/device-one/.env")
        )
        let locator = HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default)

        let resolution = try locator.resolve(helperPath: helper.path)
        let environment = locator.helperEnvironment(for: resolution, context: context)

        XCTAssertEqual(environment["TCAPSULE_CONFIG"], context.configURL.path)
        XCTAssertNotNil(environment["TCAPSULE_STATE_DIR"])
        XCTAssertNotEqual(environment["TCAPSULE_STATE_DIR"], context.configURL.deletingLastPathComponent().path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: context.configURL.deletingLastPathComponent().path))
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
        XCTAssertEqual(resolution.mode, .developmentCheckout)
        XCTAssertNil(resolution.toolsBinURL)
        XCTAssertEqual(environment["TCAPSULE_DISTRIBUTION_ROOT"], repo.path)
    }

    func testLocatorPrefersProductionBundleOverDevelopmentHelper() throws {
        let temp = try TemporaryDirectory()
        let bundle = try makeAppBundle(in: temp.url)
        let repo = try makeRepo(in: temp.url)

        let locator = HelperLocator(
            environment: ["TCAPSULE_SOURCE_ROOT": repo.path],
            currentDirectory: temp.url,
            bundle: bundle,
            fileManager: .default
        )

        let resolution = try locator.resolve(helperPath: nil)

        XCTAssertEqual(resolution.mode, .productionBundle)
        XCTAssertEqual(resolution.executableURL.path, bundle.bundleURL.appendingPathComponent("Contents/Helpers/tcapsule").path)
        XCTAssertEqual(resolution.distributionRootURL?.path, bundle.resourceURL?.appendingPathComponent("Distribution").path)
        XCTAssertEqual(resolution.toolsBinURL?.path, bundle.resourceURL?.appendingPathComponent("Tools/bin").path)
    }

    func testLocatorPrependsBundledToolsToPath() throws {
        let temp = try TemporaryDirectory()
        let bundle = try makeAppBundle(in: temp.url)
        let locator = HelperLocator(
            environment: ["PATH": "/usr/bin"],
            currentDirectory: temp.url,
            bundle: bundle,
            fileManager: .default
        )

        let resolution = try locator.resolve(helperPath: nil)
        let environment = locator.helperEnvironment(for: resolution)

        XCTAssertEqual(environment["PATH"], "\(resolution.toolsBinURL!.path):/usr/bin")
        XCTAssertEqual(environment["TCAPSULE_DISTRIBUTION_ROOT"], resolution.distributionRootURL?.path)
    }

    func testProductionRuntimeIssuesReportMissingToolsAsWarning() throws {
        let temp = try TemporaryDirectory()
        let bundle = try makeAppBundle(in: temp.url, createTools: false)
        let locator = HelperLocator(environment: [:], currentDirectory: temp.url, bundle: bundle, fileManager: .default)

        let resolution = try locator.resolve(helperPath: nil)
        let issues = locator.runtimeIssues(for: resolution)

        XCTAssertTrue(issues.contains(where: { $0.code == .toolsDirectoryMissing && $0.severity == .warning }))
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

    private func makeRepo(in directory: URL) throws -> URL {
        let repo = directory.appendingPathComponent("Repo", isDirectory: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent(".venv/bin", isDirectory: true), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent("bin", isDirectory: true), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent("src/timecapsulesmb", isDirectory: true), withIntermediateDirectories: true)
        try "".write(to: repo.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
        let helper = repo.appendingPathComponent(".venv/bin/tcapsule")
        try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)
        return repo
    }

    private func makeAppBundle(in directory: URL, createTools: Bool = true) throws -> Bundle {
        let app = directory.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true)
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        let macOS = contents.appendingPathComponent("MacOS", isDirectory: true)
        let resources = contents.appendingPathComponent("Resources", isDirectory: true)
        let helpers = contents.appendingPathComponent("Helpers", isDirectory: true)
        try FileManager.default.createDirectory(at: macOS, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        try """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>CFBundleExecutable</key>
            <string>TimeCapsuleSMB</string>
            <key>CFBundleIdentifier</key>
            <string>test.TimeCapsuleSMB</string>
            <key>CFBundlePackageType</key>
            <string>APPL</string>
        </dict>
        </plist>
        """.write(to: contents.appendingPathComponent("Info.plist"), atomically: true, encoding: .utf8)
        try "#!/bin/sh\nexit 0\n".write(to: macOS.appendingPathComponent("TimeCapsuleSMB"), atomically: true, encoding: .utf8)
        try "#!/bin/sh\nexit 0\n".write(to: helpers.appendingPathComponent("tcapsule"), atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helpers.appendingPathComponent("tcapsule").path)
        try FileManager.default.createDirectory(at: resources.appendingPathComponent("Distribution", isDirectory: true), withIntermediateDirectories: true)
        if createTools {
            try FileManager.default.createDirectory(at: resources.appendingPathComponent("Tools/bin", isDirectory: true), withIntermediateDirectories: true)
        }
        guard let bundle = Bundle(url: app) else {
            throw NSError(domain: "HelperLocatorTests", code: 1)
        }
        return bundle
    }
}

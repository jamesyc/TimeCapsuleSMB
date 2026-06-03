import XCTest
@testable import TimeCapsuleSMBApp

final class BundleLayoutTests: XCTestCase {
    func testResourceBundleLocatorPrefersPackagedResourceDirectory() throws {
        let temp = try TemporaryDirectory()
        let app = temp.url.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true)
        let packaged = app
            .appendingPathComponent("Contents/Resources", isDirectory: true)
            .appendingPathComponent(AppResourceBundleLocator.bundleDirectoryName, isDirectory: true)
        let appRoot = app.appendingPathComponent(AppResourceBundleLocator.bundleDirectoryName, isDirectory: true)
        try FileManager.default.createDirectory(at: packaged, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: appRoot, withIntermediateDirectories: true)

        let resolved = AppResourceBundleLocator.bundleURL(
            appBundleURL: app,
            resourceURL: app.appendingPathComponent("Contents/Resources", isDirectory: true)
        )

        XCTAssertEqual(resolved?.standardizedFileURL, packaged.standardizedFileURL)
    }

    func testLaunchResourceValidationLoadsLocalizedStrings() {
        XCTAssertNil(AppLaunchResourceValidation.validate())
    }

    func testLaunchResourceValidationIsIndependentOfSelectedLanguage() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)

        XCTAssertEqual(L10n.string("sidebar.activity"), "活动")
        XCTAssertNil(AppLaunchResourceValidation.validate())
    }

    func testStateInventoriesAreExplicit() {
        XCTAssertEqual(BundleRuntimeMode.allCases, [.explicit, .productionBundle, .developmentCheckout])
        XCTAssertEqual(BundleRuntimeIssueSeverity.allCases, [.warning, .error])
        XCTAssertEqual(
            BundleRuntimeIssueCode.allCases,
            [
                .helperMissing,
                .helperNotExecutable,
                .pythonPackagesMissing,
                .distributionRootMissing,
                .artifactManifestMissing,
                .artifactManifestInvalid,
                .distributionArtifactsMissing,
                .toolsDirectoryMissing,
                .applicationSupportUnavailable,
                .stateDirectoryUnavailable,
                .unsupportedVersion,
                .versionMetadataUnavailable,
                .installValidationFailed,
                .helperLaunchFailed,
                .contractDecodeFailed,
                .operationFailed
            ]
        )
    }

    func testValidProductionLayoutHasNoIssues() throws {
        let layout = try makeLayout()

        XCTAssertEqual(layout.validationIssues(), [])
    }

    func testMissingHelperIsBlockingIssue() throws {
        let layout = try makeLayout(createHelper: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .helperMissing && $0.severity == .error }))
    }

    func testNonExecutableHelperIsBlockingIssue() throws {
        let layout = try makeLayout(helperPermissions: 0o644)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .helperNotExecutable && $0.severity == .error }))
    }

    func testMissingDistributionRootIsBlockingIssue() throws {
        let layout = try makeLayout(createDistribution: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .distributionRootMissing && $0.severity == .error }))
    }

    func testMissingDistributionArtifactsIsBlockingIssue() throws {
        let layout = try makeLayout(createDistributionBin: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .distributionArtifactsMissing && $0.severity == .error }))
    }

    func testMissingArtifactManifestIsBlockingIssue() throws {
        let layout = try makeLayout(createArtifactManifest: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .artifactManifestMissing && $0.severity == .error }))
    }

    func testInvalidArtifactManifestIsBlockingIssue() throws {
        let layout = try makeLayout(artifactManifestContents: "{")

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .artifactManifestInvalid && $0.severity == .error }))
    }

    func testUnsafeArtifactManifestPathIsBlockingIssue() throws {
        let layout = try makeLayout(artifactManifestContents: """
        {
          "artifacts": {
            "one": {
              "path": "../outside"
            }
          }
        }
        """)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .artifactManifestInvalid && $0.severity == .error }))
    }

    func testManifestMissingArtifactIsBlockingIssue() throws {
        let layout = try makeLayout(createManifestArtifact: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .distributionArtifactsMissing && $0.severity == .error }))
    }

    func testMissingPythonPackagesAreBlockingIssue() throws {
        let layout = try makeLayout(createPythonPackages: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .pythonPackagesMissing && $0.severity == .error }))
    }

    func testMissingToolsDirectoryIsWarningIssue() throws {
        let layout = try makeLayout(createTools: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .toolsDirectoryMissing && $0.severity == .warning }))
    }

    func testApplicationSupportPathMustBeWritableDirectory() throws {
        let temp = try TemporaryDirectory()
        let appSupportFile = temp.url.appendingPathComponent("Application Support")
        try "not a directory".write(to: appSupportFile, atomically: true, encoding: .utf8)
        let layout = try makeLayout(applicationSupportURL: appSupportFile)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .applicationSupportUnavailable && $0.severity == .error }))
    }

    private func makeLayout(
        createHelper: Bool = true,
        helperPermissions: Int = 0o755,
        createDistribution: Bool = true,
        createDistributionBin: Bool = true,
        createArtifactManifest: Bool = true,
        artifactManifestContents: String? = nil,
        createManifestArtifact: Bool = true,
        createPythonPackages: Bool = true,
        createTools: Bool = true,
        applicationSupportURL: URL? = nil
    ) throws -> BundleLayout {
        let temp = try TemporaryDirectory()
        let app = temp.url.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true)
        let resources = app.appendingPathComponent("Contents/Resources", isDirectory: true)
        let helpers = app.appendingPathComponent("Contents/Helpers", isDirectory: true)
        let appSupport = applicationSupportURL ?? temp.url.appendingPathComponent("Application Support", isDirectory: true)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        if applicationSupportURL == nil {
            try FileManager.default.createDirectory(at: appSupport, withIntermediateDirectories: true)
        }

        let helper = helpers.appendingPathComponent("tcapsule")
        if createHelper {
            try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: helperPermissions], ofItemAtPath: helper.path)
        }
        if createDistribution {
            let distribution = resources.appendingPathComponent("Distribution", isDirectory: true)
            try FileManager.default.createDirectory(at: distribution, withIntermediateDirectories: true)
            if createDistributionBin {
                let artifactDirectory = distribution.appendingPathComponent("bin/payloads", isDirectory: true)
                try FileManager.default.createDirectory(at: artifactDirectory, withIntermediateDirectories: true)
                if createManifestArtifact {
                    try "payload".write(
                        to: artifactDirectory.appendingPathComponent("one"),
                        atomically: true,
                        encoding: .utf8
                    )
                }
            }
            if createArtifactManifest {
                let manifest = artifactManifestContents ?? """
                {
                  "artifacts": {
                    "one": {
                      "path": "bin/payloads/one"
                    }
                  }
                }
                """
                try manifest.write(
                    to: distribution.appendingPathComponent("artifact-manifest.json"),
                    atomically: true,
                    encoding: .utf8
                )
            }
        }
        if createPythonPackages {
            let pythonPackages = resources
                .appendingPathComponent("Python", isDirectory: true)
                .appendingPathComponent("site-packages", isDirectory: true)
            try FileManager.default.createDirectory(at: pythonPackages, withIntermediateDirectories: true)
        }
        if createTools {
            try FileManager.default.createDirectory(
                at: resources.appendingPathComponent("Tools/bin", isDirectory: true),
                withIntermediateDirectories: true
            )
        }
        return BundleLayout(
            appBundleURL: app,
            resourceURL: resources,
            helperURL: helper,
            applicationSupportURL: appSupport
        )
    }
}

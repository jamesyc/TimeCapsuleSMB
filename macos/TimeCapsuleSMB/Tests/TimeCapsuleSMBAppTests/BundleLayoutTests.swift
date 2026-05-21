import XCTest
@testable import TimeCapsuleSMBApp

final class BundleLayoutTests: XCTestCase {
    func testStateInventoriesAreExplicit() {
        XCTAssertEqual(BundleRuntimeMode.allCases, [.explicit, .productionBundle, .developmentCheckout])
        XCTAssertEqual(BundleRuntimeIssueSeverity.allCases, [.warning, .error])
        XCTAssertEqual(
            BundleRuntimeIssueCode.allCases,
            [
                .helperMissing,
                .helperNotExecutable,
                .distributionRootMissing,
                .toolsDirectoryMissing,
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

    func testMissingToolsDirectoryIsWarningIssue() throws {
        let layout = try makeLayout(createTools: false)

        let issues = layout.validationIssues()

        XCTAssertTrue(issues.contains(where: { $0.code == .toolsDirectoryMissing && $0.severity == .warning }))
    }

    private func makeLayout(
        createHelper: Bool = true,
        helperPermissions: Int = 0o755,
        createDistribution: Bool = true,
        createTools: Bool = true
    ) throws -> BundleLayout {
        let temp = try TemporaryDirectory()
        let app = temp.url.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true)
        let resources = app.appendingPathComponent("Contents/Resources", isDirectory: true)
        let helpers = app.appendingPathComponent("Contents/Helpers", isDirectory: true)
        let appSupport = temp.url.appendingPathComponent("Application Support", isDirectory: true)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: appSupport, withIntermediateDirectories: true)

        let helper = helpers.appendingPathComponent("tcapsule")
        if createHelper {
            try "#!/bin/sh\nexit 0\n".write(to: helper, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: helperPermissions], ofItemAtPath: helper.path)
        }
        if createDistribution {
            try FileManager.default.createDirectory(
                at: resources.appendingPathComponent("Distribution", isDirectory: true),
                withIntermediateDirectories: true
            )
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

import CryptoKit
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppUpdateInstallerTests: XCTestCase {
    private static let bundleID = "com.timecapsulesmb.TimeCapsuleSMB"
    private static let teamID = "M22Z394H44"

    /// Scripts `ditto`, `codesign` and `spctl` and records every invocation in order.
    final class FakeRunner: ProcessRunning, @unchecked Sendable {
        private(set) var calls: [(executable: String, arguments: [String])] = []
        var handler: (String, [String]) -> ProcessOutput = { _, _ in ProcessOutput(exitCode: 0, stdout: "", stderr: "") }

        func run(_ executable: String, _ arguments: [String]) async throws -> ProcessOutput {
            calls.append((executable, arguments))
            return handler((executable as NSString).lastPathComponent, arguments)
        }

        var executables: [String] {
            calls.map { ($0.executable as NSString).lastPathComponent }
        }
    }

    struct FakeDownloader: UpdateDownloading {
        let bytes: Data

        func download(_ url: URL, to destination: URL, expectedSize: Int?, progress: @escaping @Sendable (Double) -> Void) async throws {
            try bytes.write(to: destination)
            progress(1)
        }
    }

    final class Rig {
        let temp: TemporaryDirectory
        let bundleURL: URL
        let updates: URL
        let runner: FakeRunner
        var installer: AppUpdateInstaller!
        var relaunched: [URL] = []

        init(temp: TemporaryDirectory, bundleURL: URL, updates: URL, runner: FakeRunner) {
            self.temp = temp
            self.bundleURL = bundleURL
            self.updates = updates
            self.runner = runner
        }
    }

    private func makeRig(
        runtimeMode: BundleRuntimeMode = .productionBundle,
        busy: Bool = false,
        payload: Data = Data("zip-bytes".utf8),
        expandedVersion: Int? = 30002,
        newTeamID: String? = teamID,
        runningTeamID: String? = teamID,
        codesignVerifyExit: Int32 = 0,
        spctlExit: Int32 = 0
    ) throws -> Rig {
        let temp = try TemporaryDirectory()
        let apps = temp.url.appendingPathComponent("Applications", isDirectory: true)
        let bundleURL = apps.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true)
        try writeBundle(at: bundleURL, version: 30001)
        let updates = temp.url.appendingPathComponent("updates", isDirectory: true)
        let runner = FakeRunner()
        runner.handler = { executable, arguments in
            switch executable {
            case "ditto":
                // Simulate expansion by writing the new bundle where ditto would put it.
                let destination = URL(fileURLWithPath: arguments[3], isDirectory: true)
                if let expandedVersion {
                    try? self.writeBundle(at: destination.appendingPathComponent("TimeCapsuleSMB.app", isDirectory: true), version: expandedVersion)
                }
                return ProcessOutput(exitCode: 0, stdout: "", stderr: "")
            case "codesign":
                if arguments.first == "--verify" {
                    return ProcessOutput(exitCode: codesignVerifyExit, stdout: "", stderr: codesignVerifyExit == 0 ? "" : "invalid signature")
                }
                let path = arguments.last ?? ""
                let team = path == bundleURL.path ? runningTeamID : newTeamID
                return ProcessOutput(exitCode: 0, stdout: "", stderr: "Identifier=\(Self.bundleID)\nTeamIdentifier=\(team ?? "not set")\n")
            case "spctl":
                return ProcessOutput(exitCode: spctlExit, stdout: spctlExit == 0 ? "accepted" : "rejected", stderr: "")
            default:
                return ProcessOutput(exitCode: 1, stdout: "", stderr: "unexpected \(executable)")
            }
        }
        let environment = InstallEnvironment(
            bundleURL: bundleURL,
            updatesDirectory: updates,
            bundleIdentifier: Self.bundleID,
            expectedTeamID: nil,
            runtimeMode: { runtimeMode },
            hasBlockingActivity: { busy }
        )
        let rig = Rig(temp: temp, bundleURL: bundleURL, updates: updates, runner: runner)
        rig.installer = AppUpdateInstaller(
            environment: environment,
            downloader: FakeDownloader(bytes: payload),
            processRunner: runner,
            relaunch: { [weak rig] url in rig?.relaunched.append(url) }
        )
        return rig
    }

    private func writeBundle(at url: URL, version: Int) throws {
        let contents = url.appendingPathComponent("Contents", isDirectory: true)
        try FileManager.default.createDirectory(at: contents.appendingPathComponent("MacOS", isDirectory: true), withIntermediateDirectories: true)
        let plist: [String: Any] = [
            "CFBundleIdentifier": Self.bundleID,
            "CFBundleVersion": String(version),
            "CFBundleShortVersionString": "3.0.\(version % 100)"
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try data.write(to: contents.appendingPathComponent("Info.plist"))
        try Data("binary \(version)".utf8).write(to: contents.appendingPathComponent("MacOS/TimeCapsuleSMB"))
    }

    private func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func prompt(sha256: String?, versionCode: Int = 30002, withAsset: Bool = true) -> UpdatePrompt {
        UpdatePrompt(
            versionCode: versionCode,
            title: "v3.0.2",
            tag: "v3.0.2",
            notes: "",
            publishedDate: nil,
            htmlURL: URL(string: "https://example.invalid/rel"),
            asset: withAsset
                ? ReleaseAssetPayload(name: "TimeCapsuleSMB.app.zip", size: nil, downloadURL: "https://example.invalid/app.zip", sha256: sha256)
                : nil,
            isRequired: false
        )
    }

    private func installedVersion(at bundleURL: URL) -> String? {
        guard let data = try? Data(contentsOf: bundleURL.appendingPathComponent("Contents/Info.plist")),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else {
            return nil
        }
        return plist["CFBundleVersion"] as? String
    }

    // MARK: - Preconditions

    func testRefusesDevelopmentCheckout() async throws {
        let rig = try makeRig(runtimeMode: .developmentCheckout)
        let prompt = prompt(sha256: sha256(Data("zip-bytes".utf8)))

        XCTAssertEqual(rig.installer.canInstall(prompt), "In-app updates are unavailable when running from a source checkout.")
        await rig.installer.install(prompt)

        XCTAssertEqual(rig.installer.state, .failed(.unsupported("In-app updates are unavailable when running from a source checkout.")))
        XCTAssertTrue(rig.runner.calls.isEmpty)
    }

    func testRefusesWhenAssetLacksDigestOrIsMissing() throws {
        let rig = try makeRig()
        XCTAssertNotNil(rig.installer.canInstall(prompt(sha256: nil)))
        XCTAssertNotNil(rig.installer.canInstall(prompt(sha256: "ab", withAsset: false)))
        XCTAssertNil(rig.installer.canInstall(prompt(sha256: "ab")))
    }

    func testRefusesWhileDeviceOperationsRun() async throws {
        let rig = try makeRig(busy: true)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(rig.installer.state, .failed(.unsupported("Finish or cancel running device operations before updating.")))
        XCTAssertTrue(rig.runner.calls.isEmpty)
    }

    // MARK: - Verification chain

    func testDigestMismatchStopsBeforeExpansion() async throws {
        let rig = try makeRig()
        await rig.installer.install(prompt(sha256: sha256(Data("other".utf8))))

        XCTAssertEqual(rig.installer.state, .failed(.digestMismatch))
        XCTAssertTrue(rig.runner.calls.isEmpty)
        XCTAssertEqual(installedVersion(at: rig.bundleURL), "30001")
    }

    func testBundleVersionMismatchFails() async throws {
        let rig = try makeRig(expandedVersion: 30003)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        guard case .failed(.bundleInvalid(let detail)) = rig.installer.state else {
            return XCTFail("unexpected state \(rig.installer.state)")
        }
        XCTAssertTrue(detail.contains("30003"), detail)
        XCTAssertEqual(rig.runner.executables, ["ditto"])
    }

    func testMissingBundleAfterExpansionFails() async throws {
        let rig = try makeRig(expandedVersion: nil)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        guard case .failed(.bundleInvalid) = rig.installer.state else {
            return XCTFail("unexpected state \(rig.installer.state)")
        }
    }

    func testInvalidSignatureFails() async throws {
        let rig = try makeRig(codesignVerifyExit: 1)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(rig.installer.state, .failed(.signatureInvalid("invalid signature")))
        XCTAssertEqual(rig.runner.executables, ["ditto", "codesign"])
    }

    func testTeamIDMismatchFails() async throws {
        let rig = try makeRig(newTeamID: "OTHER12345")
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(rig.installer.state, .failed(.teamIDMismatch(expected: Self.teamID, actual: "OTHER12345")))
        XCTAssertFalse(rig.runner.executables.contains("spctl"))
    }

    func testAdHocRunningAppCannotInstall() async throws {
        let rig = try makeRig(runningTeamID: nil)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(
            rig.installer.state,
            .failed(.unsupported("In-app updates require a signed release build of TimeCapsuleSMB; use Download instead."))
        )
        XCTAssertEqual(rig.runner.executables, ["ditto", "codesign", "codesign"], "the new bundle's identity is not read")
    }

    func testGatekeeperRejectFails() async throws {
        let rig = try makeRig(spctlExit: 3)
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(rig.installer.state, .failed(.gatekeeperRejected("rejected")))
        XCTAssertEqual(installedVersion(at: rig.bundleURL), "30001")
        XCTAssertTrue(rig.relaunched.isEmpty)
    }

    // MARK: - Success and swap

    func testExpandsVerifiesSwapsAndRelaunchesOnSuccess() async throws {
        let rig = try makeRig()
        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        XCTAssertEqual(rig.installer.state, .readyToRelaunch)
        XCTAssertEqual(rig.runner.executables, ["ditto", "codesign", "codesign", "codesign", "spctl"])
        XCTAssertEqual(rig.runner.calls[0].arguments.prefix(2), ["-x", "-k"])
        XCTAssertEqual(rig.runner.calls[1].arguments.prefix(3), ["--verify", "--deep", "--strict"])
        XCTAssertEqual(rig.runner.calls[4].arguments.prefix(3), ["--assess", "--type", "execute"])
        XCTAssertEqual(installedVersion(at: rig.bundleURL), "30002")
        XCTAssertFalse(FileManager.default.fileExists(atPath: rig.bundleURL.deletingLastPathComponent().appendingPathComponent("TimeCapsuleSMB.app.previous").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: rig.updates.appendingPathComponent("30002").path))
        XCTAssertEqual(rig.relaunched, [rig.bundleURL])
    }

    func testSwapFailureRollsBackOriginalBundle() async throws {
        let rig = try makeRig()
        let parent = rig.bundleURL.deletingLastPathComponent()
        // Make the parent directory read-only after expansion by hooking ditto.
        let previousHandler = rig.runner.handler
        rig.runner.handler = { executable, arguments in
            let output = previousHandler(executable, arguments)
            if executable == "spctl" {
                try? FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: parent.path)
            }
            return output
        }
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: parent.path) }

        await rig.installer.install(prompt(sha256: sha256(Data("zip-bytes".utf8))))

        guard case .failed(.swapFailed) = rig.installer.state else {
            return XCTFail("unexpected state \(rig.installer.state)")
        }
        try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: parent.path)
        XCTAssertEqual(installedVersion(at: rig.bundleURL), "30001")
        XCTAssertTrue(rig.relaunched.isEmpty)
    }

    func testCleanupRemovesStaleDownloads() throws {
        let temp = try TemporaryDirectory()
        let updates = temp.url.appendingPathComponent("updates", isDirectory: true)
        try FileManager.default.createDirectory(at: updates.appendingPathComponent("1"), withIntermediateDirectories: true)
        try Data().write(to: updates.appendingPathComponent("1/TimeCapsuleSMB.app.zip"))

        AppUpdateInstaller.cleanupStaleDownloads(in: updates)

        XCTAssertFalse(FileManager.default.fileExists(atPath: updates.path))
    }

    func testDownloadHostTrustIncludesMetadataOverrideHost() {
        XCTAssertTrue(URLSessionUpdateDownloader.isTrusted(host: "objects.githubusercontent.com", additionalHosts: []))
        XCTAssertTrue(URLSessionUpdateDownloader.isTrusted(host: "GitHub.com", additionalHosts: []))
        XCTAssertFalse(URLSessionUpdateDownloader.isTrusted(host: "127.0.0.1", additionalHosts: []))
        XCTAssertTrue(URLSessionUpdateDownloader.isTrusted(host: "127.0.0.1", additionalHosts: ["127.0.0.1"]))
        XCTAssertEqual(URLSessionUpdateDownloader.trustedHost(fromMetadataURL: " http://127.0.0.1:8000/latest.json "), "127.0.0.1")
        XCTAssertEqual(URLSessionUpdateDownloader.trustedHost(fromMetadataURL: "https://Mirror.Example.net/x"), "mirror.example.net")
        XCTAssertNil(URLSessionUpdateDownloader.trustedHost(fromMetadataURL: ""))
        XCTAssertNil(URLSessionUpdateDownloader.trustedHost(fromMetadataURL: "not a url"))
    }

    func testTeamIdentifierParsing() {
        XCTAssertEqual(AppUpdateInstaller.teamIdentifier(in: "Identifier=x\nTeamIdentifier=M22Z394H44\nSealed Resources=2"), "M22Z394H44")
        XCTAssertNil(AppUpdateInstaller.teamIdentifier(in: "Identifier=x\nTeamIdentifier=not set"))
        XCTAssertNil(AppUpdateInstaller.teamIdentifier(in: "Signature=adhoc"))
    }
}

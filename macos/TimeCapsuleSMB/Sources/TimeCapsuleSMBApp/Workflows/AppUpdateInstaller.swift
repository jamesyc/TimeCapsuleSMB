import AppKit
import CryptoKit
import Foundation

enum InstallFailure: Equatable, Error {
    case unsupported(String)
    case download(String)
    case digestMismatch
    case expandFailed(String)
    case bundleInvalid(String)
    case signatureInvalid(String)
    case teamIDMismatch(expected: String?, actual: String?)
    case gatekeeperRejected(String)
    case swapFailed(String)

    var localizedMessage: String {
        switch self {
        case .unsupported(let reason):
            return reason
        case .download(let detail):
            return L10n.format("app_update.install.failed.download", detail)
        case .digestMismatch:
            return L10n.string("app_update.install.failed.digest")
        case .expandFailed:
            return L10n.string("app_update.install.failed.expand")
        case .bundleInvalid(let detail):
            return L10n.format("app_update.install.failed.bundle", detail)
        case .signatureInvalid:
            return L10n.string("app_update.install.failed.signature")
        case .teamIDMismatch:
            return L10n.string("app_update.install.failed.team")
        case .gatekeeperRejected:
            return L10n.string("app_update.install.failed.gatekeeper")
        case .swapFailed(let detail):
            return L10n.format("app_update.install.failed.swap", detail)
        }
    }
}

enum InstallState: Equatable {
    case idle
    case downloading(Double)
    case verifying
    case installing
    case readyToRelaunch
    case failed(InstallFailure)

    var isActive: Bool {
        switch self {
        case .downloading, .verifying, .installing, .readyToRelaunch:
            return true
        case .idle, .failed:
            return false
        }
    }
}

/// Where the running app lives and what the installer may assume about it.
struct InstallEnvironment {
    let bundleURL: URL
    let updatesDirectory: URL
    let bundleIdentifier: String
    /// Team ID of the running app's Developer ID signature; nil for ad-hoc or unsigned builds.
    let expectedTeamID: String?
    let runtimeMode: () -> BundleRuntimeMode
    let hasBlockingActivity: () -> Bool

    static let productionUpdatesDirectory: URL = {
        let base = BundleLayout.applicationSupportDirectory()
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/TimeCapsuleSMB", isDirectory: true)
        return base.appendingPathComponent("updates", isDirectory: true)
    }()

    @MainActor
    static func production(
        helperPathOverride: @escaping () -> String,
        hasBlockingActivity: @escaping () -> Bool
    ) -> InstallEnvironment {
        InstallEnvironment(
            bundleURL: Bundle.main.bundleURL,
            updatesDirectory: productionUpdatesDirectory,
            bundleIdentifier: Bundle.main.bundleIdentifier ?? "com.timecapsulesmb.TimeCapsuleSMB",
            expectedTeamID: nil,
            runtimeMode: {
                let override = helperPathOverride().trimmingCharacters(in: .whitespacesAndNewlines)
                return (try? HelperLocator().resolve(helperPath: override.isEmpty ? nil : override))?.mode ?? .developmentCheckout
            },
            hasBlockingActivity: hasBlockingActivity
        )
    }
}

protocol UpdateDownloading {
    func download(
        _ url: URL,
        to destination: URL,
        expectedSize: Int?,
        progress: @escaping @Sendable (Double) -> Void
    ) async throws
}

enum UpdateDownloadError: Error, LocalizedError {
    case badStatus(Int)
    case untrustedHost(String)
    case sizeMismatch(expected: Int, actual: Int)

    var errorDescription: String? {
        switch self {
        case .badStatus(let code):
            return "HTTP \(code)"
        case .untrustedHost(let host):
            return "untrusted download host \(host)"
        case .sizeMismatch(let expected, let actual):
            return "expected \(expected) bytes, received \(actual)"
        }
    }
}

/// Downloads through URLSession. GitHub redirects release assets to a CDN host, so the final
/// response host is checked against an allowlist rather than the request URL. The host of a
/// user-configured release metadata URL is trusted too: whoever supplies the digest may supply
/// the bytes, which is what makes local testing against a private server possible.
final class URLSessionUpdateDownloader: NSObject, UpdateDownloading, URLSessionDownloadDelegate, @unchecked Sendable {
    static let trustedHosts: Set<String> = [
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com"
    ]

    /// Host component of a metadata URL override, lowercased, or nil when blank or unparsable.
    static func trustedHost(fromMetadataURL urlString: String) -> String? {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let host = URL(string: trimmed)?.host?.lowercased(), !host.isEmpty else {
            return nil
        }
        return host
    }

    static func isTrusted(host: String, additionalHosts: Set<String>) -> Bool {
        let lowered = host.lowercased()
        return trustedHosts.contains(lowered) || additionalHosts.contains(lowered)
    }

    private let additionalTrustedHosts: () -> Set<String>
    private var progressHandler: (@Sendable (Double) -> Void)?
    private var expectedSize: Int?
    private var continuation: CheckedContinuation<URL, Error>?

    init(additionalTrustedHosts: @escaping () -> Set<String> = { [] }) {
        self.additionalTrustedHosts = additionalTrustedHosts
    }

    func download(
        _ url: URL,
        to destination: URL,
        expectedSize: Int?,
        progress: @escaping @Sendable (Double) -> Void
    ) async throws {
        self.progressHandler = progress
        self.expectedSize = expectedSize
        let session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let temporary: URL = try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            var request = URLRequest(url: url)
            request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
            session.downloadTask(with: request).resume()
        }
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporary, to: destination)
        if let expectedSize, expectedSize > 0 {
            let actual = (try? FileManager.default.attributesOfItem(atPath: destination.path)[.size] as? Int) ?? -1
            guard actual == expectedSize else {
                throw UpdateDownloadError.sizeMismatch(expected: expectedSize, actual: actual)
            }
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard let response = downloadTask.response as? HTTPURLResponse else {
            continuation?.resume(throwing: UpdateDownloadError.badStatus(0))
            continuation = nil
            return
        }
        guard (200..<300).contains(response.statusCode) else {
            continuation?.resume(throwing: UpdateDownloadError.badStatus(response.statusCode))
            continuation = nil
            return
        }
        let host = response.url?.host?.lowercased() ?? ""
        guard Self.isTrusted(host: host, additionalHosts: additionalTrustedHosts()) else {
            continuation?.resume(throwing: UpdateDownloadError.untrustedHost(host))
            continuation = nil
            return
        }
        // The location is deleted when this delegate method returns; move it to a stable temp path first.
        let stable = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        do {
            try FileManager.default.moveItem(at: location, to: stable)
            continuation?.resume(returning: stable)
        } catch {
            continuation?.resume(throwing: error)
        }
        continuation = nil
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64,
        totalBytesExpectedToWrite: Int64
    ) {
        let total = totalBytesExpectedToWrite > 0 ? totalBytesExpectedToWrite : Int64(expectedSize ?? 0)
        guard total > 0 else {
            return
        }
        progressHandler?(min(1, Double(totalBytesWritten) / Double(total)))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            continuation?.resume(throwing: error)
            continuation = nil
        }
    }
}

/// Downloads, verifies and installs a new app bundle in place, then relaunches.
///
/// Verification order: SHA256 digest from the GitHub asset, `ditto` expansion, bundle identifier
/// and version, `codesign --verify --strict`, Team ID equal to the running app's, `spctl` accept.
/// Quarantine attributes are never modified; notarized bundles pass Gatekeeper on their own.
@MainActor
final class AppUpdateInstaller: ObservableObject {
    @Published private(set) var state: InstallState = .idle

    private let environment: InstallEnvironment
    private let downloader: UpdateDownloading
    private let runner: ProcessRunning
    private let fileManager: FileManager
    private let relaunch: @MainActor (URL) -> Void

    init(
        environment: InstallEnvironment,
        downloader: UpdateDownloading,
        processRunner: ProcessRunning,
        fileManager: FileManager = .default,
        relaunch: @escaping @MainActor (URL) -> Void
    ) {
        self.environment = environment
        self.downloader = downloader
        self.runner = processRunner
        self.fileManager = fileManager
        self.relaunch = relaunch
    }

    /// Nil when install can proceed; otherwise a user-facing reason to fall back to Download.
    func canInstall(_ prompt: UpdatePrompt) -> String? {
        if environment.runtimeMode() == .developmentCheckout {
            return L10n.string("app_update.install.unsupported.dev_checkout")
        }
        guard let asset = prompt.asset, asset.sha256 != nil, URL(string: asset.downloadURL) != nil else {
            return L10n.string("app_update.install.unsupported.no_asset")
        }
        if !fileManager.isWritableFile(atPath: environment.bundleURL.deletingLastPathComponent().path) {
            return L10n.string("app_update.install.unsupported.read_only")
        }
        return nil
    }

    func reset() {
        state = .idle
    }

    func install(_ prompt: UpdatePrompt) async {
        if let reason = canInstall(prompt) {
            state = .failed(.unsupported(reason))
            return
        }
        if environment.hasBlockingActivity() {
            state = .failed(.unsupported(L10n.string("app_update.install.unsupported.busy")))
            return
        }
        guard let asset = prompt.asset, let expectedSHA = asset.sha256, let url = URL(string: asset.downloadURL) else {
            state = .failed(.unsupported(L10n.string("app_update.install.unsupported.no_asset")))
            return
        }
        let workDirectory = environment.updatesDirectory.appendingPathComponent(String(prompt.versionCode), isDirectory: true)
        do {
            try? fileManager.removeItem(at: workDirectory)
            try fileManager.createDirectory(at: workDirectory, withIntermediateDirectories: true)
            let zipURL = workDirectory.appendingPathComponent("TimeCapsuleSMB.app.zip")

            state = .downloading(0)
            try await downloader.download(url, to: zipURL, expectedSize: asset.size) { [weak self] fraction in
                Task { @MainActor in
                    if case .downloading = self?.state {
                        self?.state = .downloading(fraction)
                    }
                }
            }

            state = .verifying
            try verifyDigest(of: zipURL, expected: expectedSHA)
            let expanded = workDirectory.appendingPathComponent("expanded", isDirectory: true)
            try await expand(zipURL, into: expanded)
            let newBundle = try locateBundle(in: expanded, expectedVersionCode: prompt.versionCode)
            try await verifySignature(of: newBundle)
            try await verifyGatekeeper(of: newBundle)

            state = .installing
            try swap(in: newBundle)
            try? fileManager.removeItem(at: workDirectory)
            state = .readyToRelaunch
            relaunch(environment.bundleURL)
        } catch let failure as InstallFailure {
            state = .failed(failure)
        } catch {
            state = .failed(.download(error.localizedDescription))
        }
    }

    /// Removes leftovers from earlier install attempts (called on launch).
    static func cleanupStaleDownloads(in updatesDirectory: URL, fileManager: FileManager = .default) {
        try? fileManager.removeItem(at: updatesDirectory)
    }

    static func teamIdentifier(in codesignOutput: String) -> String? {
        for line in codesignOutput.split(whereSeparator: \.isNewline) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("TeamIdentifier=") else {
                continue
            }
            let value = trimmed.dropFirst("TeamIdentifier=".count).trimmingCharacters(in: .whitespaces)
            return value.isEmpty || value == "not set" ? nil : value
        }
        return nil
    }

    /// Default relaunch: wait for this process to exit, then `open` the bundle at the same path.
    @MainActor
    static func relaunchAfterExit(_ bundleURL: URL) {
        let pid = ProcessInfo.processInfo.processIdentifier
        let quotedPath = "'" + bundleURL.path.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let script = "while /bin/kill -0 \(pid) 2>/dev/null; do /bin/sleep 0.2; done; /usr/bin/open -n \(quotedPath)"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", script]
        try? process.run()
        AppCloseGuard.shared.allowTermination()
        NSApplication.shared.terminate(nil)
    }

    // MARK: - Steps

    private func verifyDigest(of url: URL, expected: String) throws {
        guard let handle = try? FileHandle(forReadingFrom: url) else {
            throw InstallFailure.download("downloaded file is unreadable")
        }
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        let actual = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        guard actual == expected.lowercased() else {
            throw InstallFailure.digestMismatch
        }
    }

    private func expand(_ zipURL: URL, into directory: URL) async throws {
        try? fileManager.removeItem(at: directory)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let output = try await runner.run("/usr/bin/ditto", ["-x", "-k", zipURL.path, directory.path])
        guard output.exitCode == 0 else {
            throw InstallFailure.expandFailed(output.stderr)
        }
    }

    private func locateBundle(in directory: URL, expectedVersionCode: Int) throws -> URL {
        let entries = (try? fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        let bundles = entries.filter { $0.pathExtension == "app" }
        guard bundles.count == 1, let bundle = bundles.first else {
            throw InstallFailure.bundleInvalid("expected exactly one .app, found \(bundles.count)")
        }
        let plistURL = bundle.appendingPathComponent("Contents/Info.plist")
        guard let data = try? Data(contentsOf: plistURL),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        else {
            throw InstallFailure.bundleInvalid("missing Info.plist")
        }
        guard plist["CFBundleIdentifier"] as? String == environment.bundleIdentifier else {
            throw InstallFailure.bundleInvalid("bundle identifier mismatch")
        }
        let version = (plist["CFBundleVersion"] as? String).flatMap(Int.init)
        guard version == expectedVersionCode else {
            throw InstallFailure.bundleInvalid("bundle version \(version.map(String.init) ?? "unknown") != \(expectedVersionCode)")
        }
        return bundle
    }

    private func verifySignature(of bundle: URL) async throws {
        let verify = try await runner.run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", bundle.path])
        guard verify.exitCode == 0 else {
            throw InstallFailure.signatureInvalid(verify.stderr)
        }
        let expected: String?
        if let configured = environment.expectedTeamID {
            expected = configured
        } else {
            let running = try await runner.run("/usr/bin/codesign", ["-dv", "--verbose=2", environment.bundleURL.path])
            expected = Self.teamIdentifier(in: running.stderr + running.stdout)
        }
        guard let expected else {
            // Ad-hoc or unsigned running app (local development build): there is no identity to pin to.
            throw InstallFailure.unsupported(L10n.string("app_update.install.unsupported.unsigned_app"))
        }
        let info = try await runner.run("/usr/bin/codesign", ["-dv", "--verbose=2", bundle.path])
        let actual = Self.teamIdentifier(in: info.stderr + info.stdout)
        guard let actual, expected == actual else {
            throw InstallFailure.teamIDMismatch(expected: expected, actual: actual)
        }
    }

    private func verifyGatekeeper(of bundle: URL) async throws {
        let output = try await runner.run("/usr/sbin/spctl", ["--assess", "--type", "execute", "--verbose=2", bundle.path])
        guard output.exitCode == 0 else {
            throw InstallFailure.gatekeeperRejected(output.stderr + output.stdout)
        }
    }

    private func swap(in newBundle: URL) throws {
        let target = environment.bundleURL
        let parent = target.deletingLastPathComponent()
        let aside = parent.appendingPathComponent("TimeCapsuleSMB.app.previous")
        try? fileManager.removeItem(at: aside)
        do {
            try fileManager.moveItem(at: target, to: aside)
        } catch {
            throw InstallFailure.swapFailed(error.localizedDescription)
        }
        do {
            try fileManager.moveItem(at: newBundle, to: target)
        } catch {
            try? fileManager.moveItem(at: aside, to: target)
            throw InstallFailure.swapFailed(error.localizedDescription)
        }
        var trashed: NSURL?
        if (try? fileManager.trashItem(at: aside, resultingItemURL: &trashed)) == nil {
            try? fileManager.removeItem(at: aside)
        }
    }
}

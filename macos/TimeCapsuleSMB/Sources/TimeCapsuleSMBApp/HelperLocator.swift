import Foundation

public struct HelperResolution: Equatable {
    public let executableURL: URL
    public let distributionRootURL: URL?
    public let toolsBinURL: URL?
    public let mode: BundleRuntimeMode
    public let attemptedPaths: [String]
}

public enum HelperLocatorError: Error, Equatable, LocalizedError {
    case notFound([String])

    public var errorDescription: String? {
        switch self {
        case .notFound(let attempts):
            let attempted = attempts.isEmpty ? "none" : attempts.joined(separator: ", ")
            return "Could not find the TimeCapsuleSMB helper. Attempted: \(attempted)"
        }
    }
}

public struct HelperLocator {
    public var environment: [String: String]
    public var currentDirectory: URL
    public var bundle: Bundle
    public var fileManager: FileManager

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        currentDirectory: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true),
        bundle: Bundle = .main,
        fileManager: FileManager = .default
    ) {
        self.environment = environment
        self.currentDirectory = currentDirectory
        self.bundle = bundle
        self.fileManager = fileManager
    }

    public func resolve(helperPath: String?) throws -> HelperResolution {
        var attempts: [String] = []
        if let explicit = normalized(helperPath) {
            return try resolveExplicitPath(explicit, attempts: &attempts)
        }
        if let fromEnvironment = normalized(environment["TCAPSULE_HELPER"]) {
            return try resolveExplicitPath(fromEnvironment, attempts: &attempts)
        }

        for candidate in bundledHelperCandidates() + devHelperCandidates() {
            attempts.append(candidate.url.path)
            if isExecutable(candidate.url) {
                return HelperResolution(
                    executableURL: candidate.url,
                    distributionRootURL: distributionRoot(for: candidate.url, mode: candidate.mode),
                    toolsBinURL: toolsBinURL(for: candidate.mode),
                    mode: candidate.mode,
                    attemptedPaths: attempts
                )
            }
        }
        throw HelperLocatorError.notFound(attempts)
    }

    public func helperEnvironment(for resolution: HelperResolution) -> [String: String] {
        var output = environment
        if let appSupport = applicationSupportDirectory() {
            try? fileManager.createDirectory(at: appSupport, withIntermediateDirectories: true)
            if output["TCAPSULE_CONFIG"] == nil {
                output["TCAPSULE_CONFIG"] = appSupport.appendingPathComponent(".env").path
            }
            if output["TCAPSULE_STATE_DIR"] == nil {
                output["TCAPSULE_STATE_DIR"] = appSupport.path
            }
        }
        if output["TCAPSULE_DISTRIBUTION_ROOT"] == nil, let distributionRoot = resolution.distributionRootURL {
            output["TCAPSULE_DISTRIBUTION_ROOT"] = distributionRoot.path
        }
        if let toolsBin = resolution.toolsBinURL, isDirectory(toolsBin) {
            output["PATH"] = pathByPrepending(toolsBin.path, to: output["PATH"])
        }
        output["PYTHONNOUSERSITE"] = "1"
        return output
    }

    public func runtimeIssues(for resolution: HelperResolution) -> [BundleRuntimeIssue] {
        guard resolution.mode == .productionBundle,
              let layout = BundleLayout.productionCandidate(bundle: bundle, fileManager: fileManager)
        else {
            return []
        }
        return layout.validationIssues(fileManager: fileManager)
    }

    private func resolveExplicitPath(_ path: String, attempts: inout [String]) throws -> HelperResolution {
        let candidate = url(forPath: path)
        attempts.append(candidate.path)
        guard isExecutable(candidate) else {
            throw HelperLocatorError.notFound(attempts)
        }
        return HelperResolution(
            executableURL: candidate,
            distributionRootURL: distributionRoot(for: candidate, mode: .explicit),
            toolsBinURL: toolsBinURL(for: .explicit),
            mode: .explicit,
            attemptedPaths: attempts
        )
    }

    private func normalized(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func url(forPath path: String) -> URL {
        if path.hasPrefix("/") {
            return URL(fileURLWithPath: path)
        }
        return currentDirectory.appendingPathComponent(path)
    }

    private func bundledHelperCandidates() -> [HelperCandidate] {
        var candidates: [HelperCandidate] = []
        if let layout = BundleLayout.productionCandidate(bundle: bundle, fileManager: fileManager) {
            candidates.append(HelperCandidate(url: layout.helperURL, mode: .productionBundle))
        }
        if let helper = bundle.url(forResource: "tcapsule", withExtension: nil, subdirectory: "Helpers") {
            candidates.append(HelperCandidate(url: helper, mode: .productionBundle))
        }
        if let helper = bundle.url(forResource: "tcapsule", withExtension: nil) {
            candidates.append(HelperCandidate(url: helper, mode: .productionBundle))
        }
        return candidates
    }

    private func devHelperCandidates() -> [HelperCandidate] {
        var roots: [URL] = []
        if let explicitRoot = normalized(environment["TCAPSULE_SOURCE_ROOT"]) {
            roots.append(url(forPath: explicitRoot))
        }
        roots.append(contentsOf: ancestorDirectories(startingAt: currentDirectory))
        return unique(roots).map {
            HelperCandidate(url: $0.appendingPathComponent(".venv/bin/tcapsule"), mode: .developmentCheckout)
        }
    }

    private func distributionRoot(for helperURL: URL, mode: BundleRuntimeMode) -> URL? {
        if let explicit = normalized(environment["TCAPSULE_DISTRIBUTION_ROOT"]) {
            return url(forPath: explicit)
        }
        if mode == .productionBundle,
           let bundled = BundleLayout.productionCandidate(bundle: bundle, fileManager: fileManager)?.distributionRootURL,
           isDirectory(bundled) {
            return bundled
        }
        if let repo = repoRoot(containing: helperURL) {
            return repo
        }
        if let bundled = bundle.resourceURL?.appendingPathComponent("Distribution"), isDirectory(bundled) {
            return bundled
        }
        return nil
    }

    private func toolsBinURL(for mode: BundleRuntimeMode) -> URL? {
        guard mode == .productionBundle else {
            return nil
        }
        return BundleLayout.productionCandidate(bundle: bundle, fileManager: fileManager)?.toolsBinURL
    }

    private func repoRoot(containing helperURL: URL) -> URL? {
        for candidate in ancestorDirectories(startingAt: helperURL.deletingLastPathComponent()) {
            if isRepoRoot(candidate) {
                return candidate
            }
        }
        return nil
    }

    private func ancestorDirectories(startingAt start: URL) -> [URL] {
        var output: [URL] = []
        var current = start.standardizedFileURL.path
        while true {
            output.append(URL(fileURLWithPath: current, isDirectory: true))
            let parent = (current as NSString).deletingLastPathComponent
            if parent == current || parent.isEmpty {
                break
            }
            current = parent
        }
        return output
    }

    private func unique(_ urls: [URL]) -> [URL] {
        var seen: Set<String> = []
        var output: [URL] = []
        for url in urls {
            let path = url.standardizedFileURL.path
            if seen.insert(path).inserted {
                output.append(url.standardizedFileURL)
            }
        }
        return output
    }

    private func isExecutable(_ url: URL) -> Bool {
        fileManager.isExecutableFile(atPath: url.path)
    }

    private func isDirectory(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        return fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
    }

    private func isRepoRoot(_ url: URL) -> Bool {
        let pyproject = url.appendingPathComponent("pyproject.toml")
        let bin = url.appendingPathComponent("bin")
        let sourcePackage = url.appendingPathComponent("src/timecapsulesmb")
        return fileManager.fileExists(atPath: pyproject.path)
            && isDirectory(bin)
            && isDirectory(sourcePackage)
    }

    private func applicationSupportDirectory() -> URL? {
        BundleLayout.applicationSupportDirectory(fileManager: fileManager)
    }

    private func pathByPrepending(_ prefix: String, to path: String?) -> String {
        guard let path, !path.isEmpty else {
            return prefix
        }
        return "\(prefix):\(path)"
    }
}

private struct HelperCandidate {
    let url: URL
    let mode: BundleRuntimeMode
}

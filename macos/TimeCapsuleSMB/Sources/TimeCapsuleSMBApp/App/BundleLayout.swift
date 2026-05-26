import Foundation

public enum BundleRuntimeMode: String, CaseIterable, Equatable, Sendable {
    case explicit
    case productionBundle
    case developmentCheckout
}

public enum BundleRuntimeIssueSeverity: String, CaseIterable, Equatable, Sendable {
    case warning
    case error
}

public enum BundleRuntimeIssueCode: String, CaseIterable, Equatable, Sendable {
    case helperMissing
    case helperNotExecutable
    case pythonRuntimeMissing
    case pythonExecutableMissing
    case distributionRootMissing
    case artifactManifestMissing
    case artifactManifestInvalid
    case distributionArtifactsMissing
    case toolsDirectoryMissing
    case applicationSupportUnavailable
    case stateDirectoryUnavailable
    case unsupportedVersion
    case versionMetadataUnavailable
    case installValidationFailed
    case helperLaunchFailed
    case contractDecodeFailed
    case operationFailed
}

public struct BundleRuntimeIssue: Identifiable, Equatable, Sendable {
    public var id: String {
        "\(code.rawValue):\(message)"
    }

    public let code: BundleRuntimeIssueCode
    public let severity: BundleRuntimeIssueSeverity
    public let message: String
    public let recovery: String

    public init(
        code: BundleRuntimeIssueCode,
        severity: BundleRuntimeIssueSeverity,
        message: String,
        recovery: String
    ) {
        self.code = code
        self.severity = severity
        self.message = message
        self.recovery = recovery
    }
}

public struct BundleLayout: Equatable, Sendable {
    public let appBundleURL: URL
    public let executableURL: URL?
    public let resourceURL: URL
    public let helperURL: URL
    public let distributionRootURL: URL
    public let artifactManifestURL: URL
    public let toolsBinURL: URL
    public let pythonRuntimeURL: URL?
    public let applicationSupportURL: URL
    public let configURL: URL
    public let stateDirectoryURL: URL

    public init(
        appBundleURL: URL,
        executableURL: URL? = nil,
        resourceURL: URL,
        helperURL: URL,
        distributionRootURL: URL? = nil,
        artifactManifestURL: URL? = nil,
        toolsBinURL: URL? = nil,
        pythonRuntimeURL: URL? = nil,
        applicationSupportURL: URL,
        configURL: URL? = nil,
        stateDirectoryURL: URL? = nil
    ) {
        self.appBundleURL = appBundleURL
        self.executableURL = executableURL
        self.resourceURL = resourceURL
        self.helperURL = helperURL
        let resolvedDistributionRoot = distributionRootURL ?? resourceURL.appendingPathComponent("Distribution", isDirectory: true)
        self.distributionRootURL = resolvedDistributionRoot
        self.artifactManifestURL = artifactManifestURL
            ?? resolvedDistributionRoot.appendingPathComponent("artifact-manifest.json")
        self.toolsBinURL = toolsBinURL ?? resourceURL.appendingPathComponent("Tools/bin", isDirectory: true)
        self.pythonRuntimeURL = pythonRuntimeURL ?? resourceURL.appendingPathComponent("Python", isDirectory: true)
        self.applicationSupportURL = applicationSupportURL
        self.configURL = configURL ?? applicationSupportURL.appendingPathComponent(".env")
        self.stateDirectoryURL = stateDirectoryURL ?? applicationSupportURL
    }

    public static func productionCandidate(
        bundle: Bundle = .main,
        fileManager: FileManager = .default,
        applicationSupportURL: URL? = nil
    ) -> BundleLayout? {
        let resources = bundle.resourceURL ?? bundle.bundleURL.appendingPathComponent("Contents/Resources", isDirectory: true)
        let helper = bundle.bundleURL
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Helpers", isDirectory: true)
            .appendingPathComponent("tcapsule")
        guard let appSupport = applicationSupportURL ?? applicationSupportDirectory(fileManager: fileManager) else {
            return nil
        }
        return BundleLayout(
            appBundleURL: bundle.bundleURL,
            executableURL: bundle.executableURL,
            resourceURL: resources,
            helperURL: helper,
            applicationSupportURL: appSupport
        )
    }

    public static func applicationSupportDirectory(fileManager: FileManager = .default) -> URL? {
        fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("TimeCapsuleSMB", isDirectory: true)
    }

    public func validationIssues(fileManager: FileManager = .default) -> [BundleRuntimeIssue] {
        var issues: [BundleRuntimeIssue] = []
        if !fileManager.fileExists(atPath: helperURL.path) {
            issues.append(BundleRuntimeIssue(
                code: .helperMissing,
                severity: .error,
                message: "The bundled TimeCapsuleSMB helper is missing.",
                recovery: "Reinstall TimeCapsuleSMB."
            ))
        } else if !fileManager.isExecutableFile(atPath: helperURL.path) {
            issues.append(BundleRuntimeIssue(
                code: .helperNotExecutable,
                severity: .error,
                message: "The bundled TimeCapsuleSMB helper is not executable.",
                recovery: "Reinstall TimeCapsuleSMB."
            ))
        }
        if let pythonRuntimeURL {
            if !isDirectory(pythonRuntimeURL, fileManager: fileManager) {
                issues.append(BundleRuntimeIssue(
                    code: .pythonRuntimeMissing,
                    severity: .error,
                    message: "The bundled Python runtime is missing.",
                    recovery: "Reinstall TimeCapsuleSMB."
                ))
            } else {
                let python = pythonRuntimeURL.appendingPathComponent("bin/python")
                if !fileManager.isExecutableFile(atPath: python.path) {
                    issues.append(BundleRuntimeIssue(
                        code: .pythonExecutableMissing,
                        severity: .error,
                        message: "The bundled Python executable is missing or not executable.",
                        recovery: "Reinstall TimeCapsuleSMB."
                    ))
                }
            }
        }
        if !isDirectory(distributionRootURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .distributionRootMissing,
                severity: .error,
                message: "The bundled TimeCapsuleSMB distribution is missing.",
                recovery: "Reinstall TimeCapsuleSMB."
            ))
        } else {
            let binURL = distributionRootURL.appendingPathComponent("bin", isDirectory: true)
            if !isDirectory(binURL, fileManager: fileManager) {
                issues.append(BundleRuntimeIssue(
                    code: .distributionArtifactsMissing,
                    severity: .error,
                    message: "The bundled TimeCapsuleSMB payload artifacts are missing.",
                    recovery: "Reinstall TimeCapsuleSMB."
                ))
            }
            issues.append(contentsOf: artifactManifestIssues(fileManager: fileManager))
        }
        if !isDirectory(toolsBinURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .toolsDirectoryMissing,
                severity: .warning,
                message: "Bundled command-line tools are missing.",
                recovery: "Some diagnostics may be unavailable until the app bundle is repaired."
            ))
        }
        if !isWritableDirectory(applicationSupportURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .applicationSupportUnavailable,
                severity: .error,
                message: "TimeCapsuleSMB cannot write its Application Support directory.",
                recovery: "Repair permissions for the TimeCapsuleSMB Application Support folder or reinstall the app."
            ))
        }
        if stateDirectoryURL != applicationSupportURL,
           !isWritableDirectory(stateDirectoryURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .stateDirectoryUnavailable,
                severity: .error,
                message: "TimeCapsuleSMB cannot write its runtime state directory.",
                recovery: "Repair permissions for the configured state directory."
            ))
        }
        return issues
    }

    private func artifactManifestIssues(fileManager: FileManager) -> [BundleRuntimeIssue] {
        guard fileManager.fileExists(atPath: artifactManifestURL.path) else {
            return [BundleRuntimeIssue(
                code: .artifactManifestMissing,
                severity: .error,
                message: "The bundled artifact manifest is missing.",
                recovery: "Reinstall TimeCapsuleSMB."
            )]
        }
        do {
            let data = try Data(contentsOf: artifactManifestURL)
            let manifest = try JSONDecoder().decode(ArtifactManifest.self, from: data)
            guard !manifest.artifactPaths.contains(where: isUnsafeArtifactPath) else {
                return [BundleRuntimeIssue(
                    code: .artifactManifestInvalid,
                    severity: .error,
                    message: "The bundled artifact manifest contains an unsafe artifact path.",
                    recovery: "Reinstall TimeCapsuleSMB."
                )]
            }
            let missing = manifest.artifactPaths.filter {
                !fileManager.fileExists(atPath: distributionRootURL.appendingPathComponent($0).path)
            }
            guard missing.isEmpty else {
                return [BundleRuntimeIssue(
                    code: .distributionArtifactsMissing,
                    severity: .error,
                    message: "The bundled TimeCapsuleSMB distribution is missing \(missing.count) payload artifact(s).",
                    recovery: "Reinstall TimeCapsuleSMB."
                )]
            }
            return []
        } catch {
            return [BundleRuntimeIssue(
                code: .artifactManifestInvalid,
                severity: .error,
                message: "The bundled artifact manifest could not be read.",
                recovery: "Reinstall TimeCapsuleSMB."
            )]
        }
    }

    private func isWritableDirectory(_ url: URL, fileManager: FileManager) -> Bool {
        do {
            try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        } catch {
            return false
        }
        guard isDirectory(url, fileManager: fileManager) else {
            return false
        }
        let probe = url.appendingPathComponent(".timecapsulesmb-write-test-\(UUID().uuidString)")
        do {
            try Data().write(to: probe)
            try? fileManager.removeItem(at: probe)
            return true
        } catch {
            return false
        }
    }

    private func isUnsafeArtifactPath(_ path: String) -> Bool {
        path.isEmpty
            || path.hasPrefix("/")
            || path.split(separator: "/").contains("..")
    }

    private func isDirectory(_ url: URL, fileManager: FileManager) -> Bool {
        var isDirectory: ObjCBool = false
        return fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
    }
}

private struct ArtifactManifest: Decodable {
    let artifacts: [String: ArtifactRecord]

    var artifactPaths: [String] {
        artifacts.values.map(\.path).sorted()
    }
}

private struct ArtifactRecord: Decodable {
    let path: String
}

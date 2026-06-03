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
    case pythonPackagesMissing
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
        "\(code.rawValue):\(messageOverride ?? ""):\(context ?? "")"
    }

    public let code: BundleRuntimeIssueCode
    public let severity: BundleRuntimeIssueSeverity
    private let messageOverride: String?
    private let recoveryOverride: String?
    private let context: String?

    public var message: String {
        messageOverride ?? Self.defaultMessage(for: code, context: context)
    }

    public var recovery: String {
        recoveryOverride ?? Self.defaultRecovery(for: code, context: context)
    }

    public init(
        code: BundleRuntimeIssueCode,
        severity: BundleRuntimeIssueSeverity,
        message: String? = nil,
        recovery: String? = nil,
        context: String? = nil
    ) {
        self.code = code
        self.severity = severity
        self.messageOverride = message
        self.recoveryOverride = recovery
        self.context = context
    }

    private static func defaultMessage(for code: BundleRuntimeIssueCode, context: String?) -> String {
        switch code {
        case .helperMissing:
            return L10n.string("bundle_issue.helper_missing.message")
        case .helperNotExecutable:
            return L10n.string("bundle_issue.helper_not_executable.message")
        case .pythonPackagesMissing:
            return L10n.string("bundle_issue.python_packages_missing.message")
        case .distributionRootMissing:
            return L10n.string("bundle_issue.distribution_root_missing.message")
        case .artifactManifestMissing:
            return L10n.string("bundle_issue.artifact_manifest_missing.message")
        case .artifactManifestInvalid:
            return L10n.string("bundle_issue.artifact_manifest_invalid.message")
        case .distributionArtifactsMissing:
            if let context, let count = Int(context) {
                return L10n.format("bundle_issue.distribution_artifacts_missing_count.message", count)
            }
            return L10n.string("bundle_issue.distribution_artifacts_missing.message")
        case .toolsDirectoryMissing:
            return L10n.string("bundle_issue.tools_directory_missing.message")
        case .applicationSupportUnavailable:
            return L10n.string("bundle_issue.application_support_unavailable.message")
        case .stateDirectoryUnavailable:
            return L10n.string("bundle_issue.state_directory_unavailable.message")
        case .unsupportedVersion:
            return L10n.string("bundle_issue.unsupported_version.message")
        case .versionMetadataUnavailable:
            return L10n.string("bundle_issue.version_metadata_unavailable.message")
        case .installValidationFailed:
            return L10n.string("bundle_issue.install_validation_failed.message")
        case .helperLaunchFailed:
            return L10n.string("bundle_issue.helper_launch_failed.message")
        case .contractDecodeFailed:
            return L10n.string("bundle_issue.contract_decode_failed.message")
        case .operationFailed:
            return L10n.string("bundle_issue.operation_failed.message")
        }
    }

    private static func defaultRecovery(for code: BundleRuntimeIssueCode, context: String?) -> String {
        switch code {
        case .helperMissing,
             .helperNotExecutable,
             .pythonPackagesMissing,
             .distributionRootMissing,
             .artifactManifestMissing,
             .artifactManifestInvalid,
             .distributionArtifactsMissing:
            return L10n.string("bundle_issue.recovery.reinstall")
        case .toolsDirectoryMissing:
            return L10n.string("bundle_issue.tools_directory_missing.recovery")
        case .applicationSupportUnavailable:
            return L10n.string("bundle_issue.application_support_unavailable.recovery")
        case .stateDirectoryUnavailable:
            return L10n.string("bundle_issue.state_directory_unavailable.recovery")
        case .unsupportedVersion:
            if let context, !context.isEmpty {
                return L10n.format("app_readiness.recovery.update_required", context)
            }
            return L10n.string("bundle_issue.unsupported_version.recovery")
        case .versionMetadataUnavailable:
            return L10n.string("app_readiness.recovery.version_metadata_unavailable")
        case .installValidationFailed:
            return L10n.string("app_readiness.recovery.install_validation_failed")
        case .helperLaunchFailed,
             .operationFailed:
            return L10n.string("app_readiness.recovery.retry_diagnostics")
        case .contractDecodeFailed:
            return L10n.string("app_readiness.recovery.contract_mismatch")
        }
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
    public let pythonPackagesURL: URL
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
        pythonPackagesURL: URL? = nil,
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
        self.pythonPackagesURL = pythonPackagesURL
            ?? resourceURL
                .appendingPathComponent("Python", isDirectory: true)
                .appendingPathComponent("site-packages", isDirectory: true)
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
                severity: .error
            ))
        } else if !fileManager.isExecutableFile(atPath: helperURL.path) {
            issues.append(BundleRuntimeIssue(
                code: .helperNotExecutable,
                severity: .error
            ))
        }
        if !isDirectory(pythonPackagesURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .pythonPackagesMissing,
                severity: .error
            ))
        }
        if !isDirectory(distributionRootURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .distributionRootMissing,
                severity: .error
            ))
        } else {
            let binURL = distributionRootURL.appendingPathComponent("bin", isDirectory: true)
            if !isDirectory(binURL, fileManager: fileManager) {
                issues.append(BundleRuntimeIssue(
                    code: .distributionArtifactsMissing,
                    severity: .error
                ))
            }
            issues.append(contentsOf: artifactManifestIssues(fileManager: fileManager))
        }
        if !isDirectory(toolsBinURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .toolsDirectoryMissing,
                severity: .warning
            ))
        }
        if !isWritableDirectory(applicationSupportURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .applicationSupportUnavailable,
                severity: .error
            ))
        }
        if stateDirectoryURL != applicationSupportURL,
           !isWritableDirectory(stateDirectoryURL, fileManager: fileManager) {
            issues.append(BundleRuntimeIssue(
                code: .stateDirectoryUnavailable,
                severity: .error
            ))
        }
        return issues
    }

    private func artifactManifestIssues(fileManager: FileManager) -> [BundleRuntimeIssue] {
        guard fileManager.fileExists(atPath: artifactManifestURL.path) else {
            return [BundleRuntimeIssue(
                code: .artifactManifestMissing,
                severity: .error
            )]
        }
        do {
            let data = try Data(contentsOf: artifactManifestURL)
            let manifest = try JSONDecoder().decode(ArtifactManifest.self, from: data)
            guard !manifest.artifactPaths.contains(where: isUnsafeArtifactPath) else {
                return [BundleRuntimeIssue(
                    code: .artifactManifestInvalid,
                    severity: .error
                )]
            }
            let missing = manifest.artifactPaths.filter {
                !fileManager.fileExists(atPath: distributionRootURL.appendingPathComponent($0).path)
            }
            guard missing.isEmpty else {
                return [BundleRuntimeIssue(
                    code: .distributionArtifactsMissing,
                    severity: .error,
                    context: "\(missing.count)"
                )]
            }
            return []
        } catch {
            return [BundleRuntimeIssue(
                code: .artifactManifestInvalid,
                severity: .error
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

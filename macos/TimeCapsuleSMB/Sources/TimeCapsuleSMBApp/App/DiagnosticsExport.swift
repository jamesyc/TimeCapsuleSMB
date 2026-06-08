import Foundation

struct DiagnosticsExportContext {
    var generatedAt: Date
    var appVersion: String
    var appBuild: String
    var applicationSupportPath: String
    var helperPath: String
    var appSettings: AppSettings
    var readinessState: AppReadinessStateKind
    var readinessVersionPayload: VersionCheckPayload?
    var capabilities: CapabilitiesPayload?
    var validation: InstallValidationPayload?
    var runtimeIssues: [BundleRuntimeIssue]
    var updateState: AppUpdateState
    var updatePayload: VersionCheckPayload?
    var updateError: BackendErrorViewModel?
    var selectedProfile: DeviceProfile?
    var activeOperations: [OperationLaneKey: ActiveOperation]
    var pendingConfirmation: PendingConfirmation?
    var events: [BackendEvent]
}

struct DiagnosticsExportBuilder {
    var maxEvents = 50

    func build(context: DiagnosticsExportContext) -> String {
        var lines: [String] = []
        lines.append("TimeCapsuleSMB Diagnostics")
        lines.append("Generated: \(format(date: context.generatedAt))")
        lines.append("")

        appendSection("App", to: &lines) { lines in
            append("Version", value: context.appVersion, to: &lines)
            append("Build", value: context.appBuild, to: &lines)
            append("Application Support", value: context.applicationSupportPath, to: &lines)
            append("Helper Override", value: context.helperPath.isEmpty ? "auto" : context.helperPath, to: &lines)
        }

        appendSection("Settings", to: &lines) { lines in
            append("Appearance", value: context.appSettings.appearance.rawValue, to: &lines)
            append("Telemetry Enabled", value: context.appSettings.telemetryEnabled, to: &lines)
            append("Raw Events Default", value: context.appSettings.showRawBackendEventsByDefault, to: &lines)
            append("Check Updates On Launch", value: context.appSettings.checkForUpdatesOnLaunch, to: &lines)
            append("Version Check URL", value: context.appSettings.versionCheckURL.isEmpty ? "auto" : context.appSettings.versionCheckURL, to: &lines)
            append("Time Machine Warnings", value: context.appSettings.timeMachineWarningsEnabled, to: &lines)
            append("Default NBNS", value: context.appSettings.defaultDeviceSettings.nbnsEnabled, to: &lines)
            append("Default SMB Browse Compatibility", value: context.appSettings.defaultDeviceSettings.smbBrowseCompatibility, to: &lines)
            append("Default Netatalk Metadata", value: context.appSettings.defaultDeviceSettings.fruitMetadataNetatalk, to: &lines)
            append("Default Debug Logging", value: context.appSettings.defaultDeviceSettings.debugLogging, to: &lines)
            append("Default Mount Wait", value: context.appSettings.defaultDeviceSettings.mountWaitSeconds, to: &lines)
            append("Default ATA Idle", value: context.appSettings.defaultDeviceSettings.ataIdleSeconds, to: &lines)
            append("Default ATA Standby", value: context.appSettings.defaultDeviceSettings.ataStandby.map(String.init) ?? "device default", to: &lines)
        }

        appendSection("Readiness", to: &lines) { lines in
            append("State", value: context.readinessState.title, to: &lines)
            if let version = context.readinessVersionPayload {
                append("Version Check", value: "\(version.summary) Source: \(version.source)", to: &lines)
            }
            if let capabilities = context.capabilities {
                append("Helper Version", value: "\(capabilities.helperVersion) (\(capabilities.helperVersionCode))", to: &lines)
                append("Distribution Root", value: capabilities.distributionRoot, to: &lines)
                append("Artifact Manifest SHA256", value: capabilities.artifactManifestSHA256 ?? "missing", to: &lines)
                append("Operations", value: capabilities.operations.sorted().joined(separator: ", "), to: &lines)
            }
            if let validation = context.validation {
                append("Validation", value: validation.summary, to: &lines)
                append("Validation Counts", value: sortedDescription(validation.counts), to: &lines)
                for check in validation.checks {
                    append("Check \(check.id)", value: "\(check.ok ? "PASS" : "FAIL") - \(check.message)", to: &lines)
                }
            }
            if context.runtimeIssues.isEmpty {
                append("Runtime Issues", value: "none", to: &lines)
            } else {
                for issue in context.runtimeIssues {
                    append("Runtime Issue", value: "\(issue.severity.rawValue)/\(issue.code.rawValue): \(issue.message) Recovery: \(issue.recovery)", to: &lines)
                }
            }
        }

        appendSection("Updates", to: &lines) { lines in
            append("State", value: context.updateState.title, to: &lines)
            if let payload = context.updatePayload {
                append("Summary", value: payload.summary, to: &lines)
                append("Source", value: payload.source, to: &lines)
                append("Local Version Code", value: payload.localVersionCode, to: &lines)
                append("Current Version", value: payload.currentVersion.map(String.init) ?? "unknown", to: &lines)
                append("Minimum Supported Version", value: payload.minSupportedVersion.map(String.init) ?? "unknown", to: &lines)
                append("Latest Tag", value: payload.latestTag ?? "unknown", to: &lines)
                append("Download URL", value: payload.downloadURL, to: &lines)
            }
            if let error = context.updateError {
                append("Error", value: "\(error.operation) \(error.code): \(error.message)", to: &lines)
            }
        }

        appendSection("Selected Device", to: &lines) { lines in
            if let profile = context.selectedProfile {
                append("ID", value: profile.id, to: &lines)
                append("Name", value: profile.title, to: &lines)
                append("Host", value: profile.displayTarget, to: &lines)
                append("Model", value: profile.model ?? "unknown", to: &lines)
                append("SYAP", value: profile.syap ?? "unknown", to: &lines)
                append("OS", value: [profile.osName, profile.osRelease].compactMap { $0 }.joined(separator: " ").nilIfEmpty ?? "unknown", to: &lines)
                append("Arch", value: profile.arch ?? "unknown", to: &lines)
                append("Payload Family", value: profile.payloadFamily ?? "unknown", to: &lines)
                append("Password State", value: profile.passwordState.title, to: &lines)
                append("Last Checkup", value: profile.lastCheckup?.summary ?? "none", to: &lines)
                append("Runtime State", value: profile.runtimeState?.localizedSummary ?? "unknown", to: &lines)
                append("Last Deploy", value: profile.lastDeployState?.localizedSummary ?? "none", to: &lines)
            } else {
                append("Selected", value: "none", to: &lines)
            }
        }

        appendSection("Operations", to: &lines) { lines in
            if context.activeOperations.isEmpty {
                append("Active", value: "none", to: &lines)
            } else {
                for key in context.activeOperations.keys.sorted(by: { $0.description < $1.description }) {
                    guard let operation = context.activeOperations[key] else { continue }
                    append("Active \(key.description)", value: operation.operation, to: &lines)
                }
            }
            if let confirmation = context.pendingConfirmation {
                append("Pending Confirmation", value: "\(confirmation.operation): \(confirmation.title)", to: &lines)
            } else {
                append("Pending Confirmation", value: "none", to: &lines)
            }
        }

        appendSection("Backend Events", to: &lines) { lines in
            let boundedEvents = context.events.suffix(maxEvents)
            if boundedEvents.isEmpty {
                append("Events", value: "none", to: &lines)
            } else {
                for event in boundedEvents {
                    append("Event", value: eventSummary(event), to: &lines)
                }
            }
        }

        return lines.joined(separator: "\n")
    }

    private func appendSection(_ title: String, to lines: inout [String], body: (inout [String]) -> Void) {
        lines.append("## \(title)")
        body(&lines)
        lines.append("")
    }

    private func append(_ label: String, value: Bool, to lines: inout [String]) {
        append(label, value: value ? "true" : "false", to: &lines)
    }

    private func append(_ label: String, value: Int, to lines: inout [String]) {
        append(label, value: String(value), to: &lines)
    }

    private func append(_ label: String, value: String, to lines: inout [String]) {
        lines.append("- \(label): \(redacted(value, key: label))")
    }

    private func eventSummary(_ event: BackendEvent) -> String {
        var parts = [
            event.type,
            event.operation,
            event.code,
            event.stage,
            event.status,
            event.message
        ].compactMap { $0?.nilIfEmpty }
        if let payload = event.payload {
            parts.append("payload=\(redacted(payload, key: "payload").compactDisplayText)")
        }
        if let details = event.details {
            parts.append("details=\(redacted(details, key: "details").compactDisplayText)")
        }
        if let debug = event.debug {
            parts.append("debug=\(redacted(debug, key: "debug").compactDisplayText)")
        }
        return parts.joined(separator: " | ")
    }

    private func redacted(_ value: JSONValue, key: String?) -> JSONValue {
        if shouldRedact(key: key) {
            return .string("<redacted>")
        }
        switch value {
        case .object(let object):
            return .object(object.mapValuesWithKeys { childKey, childValue in
                redacted(childValue, key: childKey)
            })
        case .array(let values):
            return .array(values.map { redacted($0, key: key) })
        default:
            return value
        }
    }

    private func redacted(_ value: String, key: String?) -> String {
        shouldRedact(key: key) ? "<redacted>" : value
    }

    private func shouldRedact(key: String?) -> Bool {
        guard let key = key?.lowercased() else {
            return false
        }
        return key.contains("password")
            || key.contains("token")
            || key.contains("secret")
            || key.contains("authorization")
            || key.contains("api_key")
            || key.contains("apikey")
            || key.contains("private_key")
            || key.contains("privatekey")
            || key.contains("credentials")
    }

    private func sortedDescription(_ values: [String: Int]) -> String {
        values.keys.sorted().map { "\($0)=\(values[$0] ?? 0)" }.joined(separator: ", ")
    }

    private func format(date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }
}

private extension JSONValue {
    var compactDisplayText: String {
        guard let data = try? JSONEncoder.sortedCompact.encode(self),
              let text = String(data: data, encoding: .utf8)
        else {
            return displayText
        }
        return text
    }
}

private extension JSONEncoder {
    static var sortedCompact: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

private extension Dictionary {
    func mapValuesWithKeys<T>(_ transform: (Key, Value) -> T) -> [Key: T] {
        Dictionary<Key, T>(uniqueKeysWithValues: map { element in
            (element.key, transform(element.key, element.value))
        })
    }
}

private extension String {
    var nilIfEmpty: String? {
        isEmpty ? nil : self
    }
}

import Foundation

/// A value substituted into a summary template: a count or a literal such as
/// a firmware version or a path. Never an English word.
enum BackendSummaryArgument: Codable, Equatable, Sendable {
    case int(Int)
    case string(String)

    init?(json: JSONValue) {
        switch json {
        case .number(let number) where number == number.rounded() && abs(number) < 1e15:
            self = .int(Int(number))
        case .string(let string):
            self = .string(string)
        default:
            return nil
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else {
            self = .string(try container.decode(String.self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .int(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        }
    }
}

/// A summary line as the helper sent it: the English text plus, when the
/// helper provides one, a catalog key and positional arguments that translate
/// it. The key registry lives in `src/timecapsulesmb/core/summaries.py`.
///
/// Formatting is checked against the template before calling
/// `String(format:)`, since a count/type mismatch there reads the wrong
/// memory. Any mismatch, or a key missing from the catalog, shows the
/// English text instead.
struct BackendSummary: Codable, Equatable, Sendable {
    /// Full catalog key, for example `backend.summary.fsck_failed`.
    let key: String?
    let arguments: [BackendSummaryArgument]
    let text: String

    init(key: String?, arguments: [BackendSummaryArgument] = [], text: String) {
        self.key = key
        self.arguments = arguments
        self.text = text
    }

    /// Builds a summary from the helper's `summary_key`/`summary_args`, or its
    /// `message_key`/`message_args` for a progress log.
    static func backend(key: String?, arguments: [JSONValue]?, text: String) -> BackendSummary {
        let values = (arguments ?? []).map(BackendSummaryArgument.init(json:))
        guard let key, !key.isEmpty, !values.contains(nil) else {
            return BackendSummary(key: nil, text: text)
        }
        return BackendSummary(key: "backend.summary.\(key)", arguments: values.compactMap { $0 }, text: text)
    }

    init?(payload: JSONValue?) {
        guard let payload else {
            return nil
        }
        let text = ["summary", "message", "summary_text"].lazy.compactMap { payload.stringValue(for: $0) }.first
        let key = payload.stringValue(for: "summary_key")
        guard text != nil || key != nil else {
            return nil
        }
        var arguments: [JSONValue]?
        if case .object(let values) = payload, case .array(let array)? = values["summary_args"] {
            arguments = array
        }
        self = .backend(key: key, arguments: arguments, text: text ?? "")
    }

    /// A summary saved in a device profile: its stored form, or, for
    /// profiles saved before summary keys existed, the key its English text
    /// maps to. Other old text (already translated when it was saved) is
    /// shown as is; empty text means there is no summary.
    static func saved(_ summary: BackendSummary?, text: String) -> BackendSummary? {
        if let summary {
            return summary
        }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        return LegacySummaryText.summary(for: trimmed) ?? BackendSummary(key: nil, text: trimmed)
    }

    var localized: String {
        localized(language: L10n.currentLanguage)
    }

    func localized(language: AppLanguage) -> String {
        resolved(language: language) ?? text
    }

    /// The translated sentence, or nil when there is no key, the catalog lacks
    /// it, or its placeholders do not match the arguments.
    func resolved(language: AppLanguage) -> String? {
        guard let key else {
            return nil
        }
        let template = L10n.string(key, language: language)
        guard template != key else {
            return nil
        }
        return Self.format(template, arguments: arguments, locale: language.locale)
    }

    enum Placeholder: Equatable {
        /// `%d`, `%i`, `%u`: a 32-bit C int.
        case int32
        /// `%ld`, `%lld`, and `%#@name@` plural variables: a 64-bit Int.
        case int
        /// `%@`: an object.
        case object
    }

    static func format(_ template: String, arguments: [BackendSummaryArgument], locale: Locale) -> String? {
        guard let placeholders = placeholders(in: template), placeholders.count == arguments.count else {
            return nil
        }
        var values: [CVarArg] = []
        for (placeholder, argument) in zip(placeholders, arguments) {
            switch (placeholder, argument) {
            case (.int32, .int(let value)):
                values.append(Int32(clamping: value))
            case (.int, .int(let value)):
                values.append(value)
            case (.object, .string(let value)):
                values.append(value as NSString)
            default:
                return nil
            }
        }
        // A plural template (%#@name@, from Localizable.stringsdict) carries its
        // plural rules on the string object the bundle returned, so it must be
        // passed here as is: a copy loses them and prints the variable raw, or
        // crashes. Foundation picks the plural form from `locale`.
        let formatted = String(format: template, locale: locale, arguments: values)
        return formatted.contains("%#@") ? nil : formatted
    }

    private static let placeholderPattern = try! NSRegularExpression(
        pattern: #"%(?:([1-9][0-9]*)\$)?(#@[A-Za-z0-9_]+@|l{0,2}[diu]|@)"#
    )

    /// The placeholder for each argument position, or nil for a template
    /// with anything `format` cannot pass safely: an unknown specifier,
    /// mixed positional and sequential forms, a skipped position, or one
    /// position used with two types.
    static func placeholders(in template: String) -> [Placeholder]? {
        let body = template.replacingOccurrences(of: "%%", with: "")
        let range = NSRange(body.startIndex..., in: body)
        let matches = placeholderPattern.matches(in: body, range: range)
        guard matches.count == body.filter({ $0 == "%" }).count else {
            return nil
        }
        var sequential: [Placeholder] = []
        var positional: [Int: Placeholder] = [:]
        for match in matches {
            guard let specifierRange = Range(match.range(at: 2), in: body) else {
                return nil
            }
            let specifier = body[specifierRange]
            let placeholder: Placeholder
            if specifier == "@" {
                placeholder = .object
            } else if specifier.hasPrefix("l") || specifier.hasPrefix("#@") {
                placeholder = .int
            } else {
                placeholder = .int32
            }
            if let positionRange = Range(match.range(at: 1), in: body), let position = Int(body[positionRange]) {
                if let existing = positional[position], existing != placeholder {
                    return nil
                }
                positional[position] = placeholder
            } else {
                sequential.append(placeholder)
            }
        }
        if positional.isEmpty {
            return sequential
        }
        guard sequential.isEmpty, positional.keys.sorted() == Array(1...positional.count) else {
            return nil
        }
        return (1...positional.count).compactMap { positional[$0] }
    }
}

/// A decoded helper payload that carries its own summary key.
protocol BackendSummarized {
    var summary: String { get }
    var summaryKey: String? { get }
    var summaryArgs: [JSONValue]? { get }
}

extension BackendSummarized {
    var summaryRef: BackendSummary {
        .backend(key: summaryKey, arguments: summaryArgs, text: summary)
    }

    var localizedSummary: String {
        summaryRef.localized
    }
}

extension CapabilitiesPayload: BackendSummarized {}
extension InstallValidationPayload: BackendSummarized {}
extension VersionCheckPayload: BackendSummarized {}
extension ReachabilityPayload: BackendSummarized {}
extension DiscoverPayload: BackendSummarized {}
extension ConfigurePayload: BackendSummarized {}
extension DeployResultPayload: BackendSummarized {}
extension DoctorPayload: BackendSummarized {}
extension ActivationResultPayload: BackendSummarized {}
extension MaintenanceResultPayload: BackendSummarized {}
extension FsckVolumeListPayload: BackendSummarized {}
extension FsckPlanPayload: BackendSummarized {}
extension FsckResultPayload: BackendSummarized {}
extension WipeDiskResultPayload: BackendSummarized {}
extension FlashBackupPayload: BackendSummarized {}
extension FlashPlanPayload: BackendSummarized {}
extension FlashWritePayload: BackendSummarized {}
extension SSHAccessPayload: BackendSummarized {}

extension DoctorCheckPayload {
    /// `localNetworkFacts` is looked up only for the block line.
    func localizedMessage(localNetworkFacts: @autoclosure () -> LocalNetworkAppFacts = .empty) -> String {
        if let problem = DoctorSummary.payloadProblem(code: code) {
            return L10n.string(problem.detailKey)
        }
        switch code {
        case "metadata_migration_in_progress":
            return L10n.string("doctor.check.metadata_migration_in_progress")
        case "disk_not_mounting":
            return L10n.string("doctor.check.disk_not_mounting")
        // The manager has not taken over the disks from Apple's diskd: the
        // helper's line names its phase, which the catalog leaves out.
        case "diskd_handoff_pending":
            return L10n.string("doctor.check.diskd_handoff_pending")
        case "diskd_restart_needed":
            return L10n.string("doctor.check.diskd_restart_needed")
        // A failed Seagate internal disk, whatever the other disks hold.
        case "disk_seagate_failed":
            return L10n.string("doctor.check.disk_seagate_failed")
        // No disk has an HFS volume: the helper's line names the disk and
        // what gpt found; the catalog's says what to do.
        case "disk_size_misreported":
            return L10n.string("doctor.check.disk_size_misreported")
        case "disk_no_hfs_partition":
            return L10n.string("doctor.check.disk_no_hfs_partition")
        // A volume AirPort Utility's erase left only in memory: the helper's
        // line names it and its disk; the catalog's says what is lost and why.
        case "disk_volumes_in_memory":
            return L10n.string("doctor.check.disk_volumes_in_memory")
        // The helper's line quotes the kernel's; the catalog's says what it means.
        case "disk_failing":
            return L10n.string("doctor.check.disk_failing")
        // The helper's line quotes the kernel's and names the disk; the
        // catalog has one sentence per disk it could name.
        case "disk_errors_logged":
            return L10n.string("doctor.check.disk_errors_logged")
        case "disk_errors_logged_internal":
            return L10n.string("doctor.check.disk_errors_logged_internal")
        case "disk_errors_logged_usb":
            return L10n.string("doctor.check.disk_errors_logged_usb")
        case "debug_logging_on":
            return L10n.string("doctor.check.debug_logging_on")
        // The device joins or extends another Wi-Fi network (wpa_supplicant runs).
        case "wifi_client":
            return L10n.string("doctor.check.wifi_client")
        // The process listing timed out; on a device on Wi-Fi the line names
        // the link as a possible cause, otherwise the device's own.
        case "process_listing_timed_out":
            if case .object(let values) = details, values["wifi_client"] == .bool(true) {
                return L10n.string("doctor.check.process_listing_timed_out_wifi")
            }
            return L10n.string("doctor.check.process_listing_timed_out")
        // The helper's line names the stuck processes; the catalog's says
        // what it means and what to do. A stuck disk check gives a running
        // repair a few more minutes before the unplug.
        case "disk_not_responding":
            if case .object(let values) = details, values["disk_check"] == .bool(true) {
                return L10n.string("doctor.check.disk_check_not_responding")
            }
            return L10n.string("doctor.check.disk_not_responding")
        // A disk check still running, which explains a volume missing meanwhile.
        case "disk_check_running":
            guard let programs = details.stringValue(for: "programs") else { return message }
            return L10n.format("doctor.check.disk_check_running", programs)
        case "client_on_unshared_network":
            return L10n.string("doctor.check.client_on_unshared_network")
        // The helper's text names ports and the connect error; the dashboard
        // shows the moved-device or SSH banner beside this row.
        case "ssh_disabled":
            return L10n.string("doctor.check.ssh_disabled")
        case "device_unreachable":
            return L10n.string("doctor.check.device_unreachable")
        // Nothing answers, judged by this Mac's router toward the device
        // (checks.network.silent_device): the same network, or another one.
        case "device_silent":
            return L10n.string("doctor.check.device_silent")
        case "device_off_network":
            return L10n.string("doctor.check.device_off_network")
        // The helper's line says how long ago the restart was asked for.
        case "device_restarting":
            return L10n.string("doctor.check.device_restarting")
        // What the app sees about itself goes after the line's own steps. A
        // Checkup result has no recovery card to carry the helper's
        // local_network_block mark, so the line goes by its code.
        case "acp_no_route":
            return L10n.sentences([L10n.string("doctor.check.acp_no_route")] + localNetworkFacts().localizedSteps)
        case "link_local_interface_gone":
            return L10n.string("doctor.check.link_local_interface_gone")
        case "mac_smb3_disabled":
            guard let path = details.stringValue(for: "path") else { return message }
            return L10n.format("doctor.check.mac_smb3_disabled", path)
        case "mac_smb3_disabled_for_server":
            guard let path = details.stringValue(for: "path"), let section = details.stringValue(for: "section") else {
                return message
            }
            return L10n.format("doctor.check.mac_smb3_disabled_for_server", path, section)
        default:
            return message
        }
    }
}

/// Maps the English summaries that profiles saved before summary keys existed
/// to their keys, so an old "last install" line still follows the app
/// language. New snapshots store a `BackendSummary` instead.
enum LegacySummaryText {
    static func summary(for text: String) -> BackendSummary? {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized.hasPrefix("netbsd4 activation complete.") {
            return BackendSummary(key: "backend.summary.activation_completed_followup", text: text)
        }
        guard let key = keys[normalized] else {
            return nil
        }
        return BackendSummary(key: "backend.summary.\(key)", text: text)
    }

    private static let keys: [String: String] = [
        "deployment completed.": "deploy_completed",
        "runtime activation complete.": "deploy_completed",
        "netbsd4 payload was already active.": "activation_already_active",
        "netbsd4 activation completed.": "activation_completed",
        "doctor checks passed.": "doctor_checks_passed",
        "doctor found one or more fatal problems.": "doctor_found_fatal",
    ]
}

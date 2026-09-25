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
extension RepairXattrsPayload: BackendSummarized {}
extension FlashBackupPayload: BackendSummarized {}
extension FlashPlanPayload: BackendSummarized {}
extension FlashWritePayload: BackendSummarized {}
extension SSHAccessPayload: BackendSummarized {}

extension DoctorCheckPayload {
    var localizedMessage: String {
        switch code {
        case "device_starting_up":
            return L10n.string("doctor.check.device_starting_up")
        case "payload_missing_from_disk":
            return L10n.string("doctor.check.payload_missing_from_disk")
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

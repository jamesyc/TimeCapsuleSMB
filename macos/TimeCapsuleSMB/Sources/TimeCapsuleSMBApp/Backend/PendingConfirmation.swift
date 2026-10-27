import Foundation

struct PendingConfirmation: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let actionTitle: String
    let operation: String
    let params: [String: JSONValue]
    let requestID: String
    let context: DeviceRuntimeContext?
    /// The operation cannot help (a failing disk's warning): Cancel is the
    /// default button.
    let prefersCancel: Bool

    init?(
        confirmationEvent event: BackendEvent,
        originalParams: [String: JSONValue],
        requestID: String = UUID().uuidString,
        context: DeviceRuntimeContext? = nil
    ) {
        guard
            event.type == "error",
            event.code == "confirmation_required",
            case .object(let details)? = event.details,
            case .string(let confirmationId)? = details["confirmation_id"]
        else {
            return nil
        }

        let presentation = ConfirmationPresentation(details: details)
        // A warning that the disk is failing, from the kernel's errors or from
        // files it could not read, and every Wipe Internal Disk confirmation
        // (it deletes everything on the disk): Cancel is the default.
        let presentationID = Self.detailString(details, "presentation_id") ?? ""
        self.prefersCancel = presentationID.hasSuffix(".disk_failing")
            || presentationID.hasSuffix(".disk_unreadable")
            || presentationID.hasPrefix("wipe-disk.")
        self.title = presentation?.title
            ?? Self.detailString(details, "title")
            ?? L10n.string("confirm.backend.title")
        self.message = presentation?.message
            ?? Self.detailString(details, "message")
            ?? event.message
            ?? L10n.string("confirm.backend.message")
        self.actionTitle = presentation?.actionTitle
            ?? Self.detailString(details, "action_title")
            ?? L10n.string("action.confirm")
        self.operation = event.operation
        self.requestID = event.requestId ?? requestID
        var confirmedParams = originalParams
        confirmedParams["confirmation_id"] = .string(confirmationId)
        self.params = confirmedParams
        self.context = context
    }

    private static func detailString(_ details: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = details[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }
}

private struct ConfirmationPresentation {
    let title: String
    let message: String
    let actionTitle: String

    init?(details: [String: JSONValue]) {
        guard
            let presentationKey = Self.detailString(details, "presentation_id"),
            let title = Self.localizedString("confirm.\(presentationKey).title"),
            let message = Self.localizedMessage(for: presentationKey, details: details)
        else {
            return nil
        }
        self.title = title
        self.message = message
        self.actionTitle = Self.localizedString("confirm.\(presentationKey).action")
            ?? Self.detailString(details, "action_title")
            ?? L10n.string("action.confirm")
    }

    private static func localizedMessage(for presentationKey: String, details: [String: JSONValue]) -> String? {
        let messageKey = "confirm.\(presentationKey).message"
        guard let template = localizedString(messageKey) else {
            return nil
        }
        let values = detailObject(details, "presentation_values")
        switch presentationKey {
        case "configure.enable_ssh_reboot",
             "ssh_access.enable_reboot",
             "deploy.netbsd4",
             "deploy.netbsd4_no_wait",
             "deploy.reboot",
             "deploy.reboot_no_wait":
            guard let deviceName = stringValue(values, "device_name") else {
                return nil
            }
            return format(template, deviceName)
        case "flash.patch_write", "flash.restore_secondary_write":
            guard let host = stringValue(values, "host") else {
                return nil
            }
            return format(template, host)
        case "flash.restore_write":
            guard let host = stringValue(values, "host") else {
                return nil
            }
            let targetBank = stringValue(values, "target_bank") ?? "target"
            return format(template, targetBank, host)
        case "wipe-disk.confirm":
            return wipeDiskMessage(template: template, values: values)
        case "wipe-disk.disk_failing", "wipe-disk.disk_unreadable":
            // The warning follows what the wipe deletes; it never replaces it.
            guard let wipeTemplate = localizedString("confirm.wipe-disk.confirm.message"),
                  let wipe = wipeDiskMessage(template: wipeTemplate, values: values) else {
                return template
            }
            return wipe + "\n\n" + template
        default:
            return template
        }
    }

    /// Wipe Internal Disk's confirmation: the disk by Apple's label and size,
    /// then what is on it. The helper sends data (sizes in MiB, format codes),
    /// worded and formatted here in the user's language.
    private static func wipeDiskMessage(template: String, values: [String: JSONValue]) -> String? {
        guard let disk = stringValue(values, "disk") else {
            return nil
        }
        let named = intValue(values, "size_mib").map { "\(disk) (\(DiskSize.decimalSize($0)))" } ?? disk
        let message = format(template, named)
        guard let contents = wipeDiskContents(values) else {
            return message
        }
        return message + " " + contents
    }

    /// Its named volumes with their used space; without any, its partition
    /// table in words (a table AirPort Utility cannot replace, no partitions,
    /// or the formats users know). Format codes without a name (EFI, Apple
    /// HFS, unknown) are left out.
    private static func wipeDiskContents(_ values: [String: JSONValue]) -> String? {
        let volumes = arrayValue(values, "volumes").compactMap { item -> String? in
            guard case .object(let volume) = item, let name = stringValue(volume, "name") else {
                return nil
            }
            guard let used = intValue(volume, "used_mib"),
                  let usedTemplate = localizedString("confirm.wipe-disk.confirm.volume_used") else {
                return name
            }
            return format(usedTemplate, name, DiskSize.decimalSize(used))
        }
        if !volumes.isEmpty {
            return localizedString("confirm.wipe-disk.confirm.volumes").map {
                format($0, ListFormatter.localizedString(byJoining: volumes))
            }
        }
        switch stringValue(values, "gpt_verdict") {
        case "bogus_map":
            return localizedString("confirm.wipe-disk.confirm.bogus_map")
        case "blank":
            return localizedString("confirm.wipe-disk.confirm.blank")
        case "foreign":
            var names: [String] = []
            for case .string(let code) in arrayValue(values, "formats") {
                if let name = localizedString("maintenance.wipe_disk.format.\(code)"), !names.contains(name) {
                    names.append(name)
                }
            }
            guard !names.isEmpty, let formatsTemplate = localizedString("confirm.wipe-disk.confirm.formats") else {
                return nil
            }
            return format(formatsTemplate, ListFormatter.localizedString(byJoining: names))
        default:
            return nil
        }
    }

    private static func localizedString(_ key: String) -> String? {
        let value = L10n.string(key)
        return value == key ? nil : value
    }

    private static func format(_ template: String, _ arguments: CVarArg...) -> String {
        String(format: template, locale: Locale.current, arguments: arguments)
    }

    private static func detailString(_ details: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = details[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }

    private static func detailObject(_ details: [String: JSONValue], _ key: String) -> [String: JSONValue] {
        guard case .object(let values)? = details[key] else {
            return [:]
        }
        return values
    }

    private static func stringValue(_ values: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = values[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }

    private static func intValue(_ values: [String: JSONValue], _ key: String) -> Int? {
        guard case .number(let value)? = values[key], value >= 0, value == value.rounded() else {
            return nil
        }
        return Int(value)
    }

    private static func arrayValue(_ values: [String: JSONValue], _ key: String) -> [JSONValue] {
        guard case .array(let items)? = values[key] else {
            return []
        }
        return items
    }
}

/// Disk sizes in Wipe Internal Disk's confirmation.
enum DiskSize {
    /// A size in MiB the way disks are sold (3950 MiB is "4.1 GB"), in the
    /// user's language and units.
    static func decimalSize(_ mib: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(mib) * 1_048_576, countStyle: .file)
    }
}

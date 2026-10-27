import Foundation

struct OperationTimelineItem: Equatable, Identifiable {
    enum State: String, Equatable {
        case pending
        case running
        case succeeded
        case warning
        case failed
    }

    let id: String
    let operation: String
    let title: String
    let detail: String?
    let state: State
    let risk: String?
    let cancellable: Bool?
    /// Shown under the detail while the step runs, without the running animation.
    var note: String? = nil
    /// Set while a long step runs: the row counts its time from here.
    var elapsedSince: Date? = nil
}

enum OperationTimelineBuilder {
    /// The note a running stage shows, with how long it has run, when it can
    /// take long enough that someone may unplug the device: Disk Repair can
    /// spend an hour on one step, and Wipe Internal Disk minutes zeroing a
    /// failing disk or waiting for the device to format it.
    private static func longDeviceStepNote(operation: String, stage: String?) -> String? {
        switch (operation, stage) {
        case ("fsck", "run_fsck"):
            return L10n.string("timeline.fsck.keep_plugged_in")
        case ("wipe-disk", "wipe_disk"), ("wipe-disk", "wait_for_erase"):
            return L10n.string("timeline.wipe_disk.keep_plugged_in")
        default:
            return nil
        }
    }

    static func timeline(from events: [BackendEvent]) -> [OperationTimelineItem] {
        events.enumerated().compactMap { index, event in
            switch event.type {
            case "stage":
                let state = stageState(forEventAt: index, in: events)
                let note = state == .running ? longDeviceStepNote(operation: event.operation, stage: event.stage) : nil
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):\(event.stage ?? "stage")",
                    operation: event.operation,
                    title: stageTitle(for: event.operation, stage: event.stage),
                    detail: latestProgressDetail(forStageAt: index, in: events)
                        ?? stageDetail(for: event.operation, stage: event.stage),
                    state: state,
                    risk: event.risk,
                    cancellable: event.cancellable,
                    note: note,
                    elapsedSince: note != nil ? event.receivedAt : nil
                )
            case "result":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):result",
                    operation: event.operation,
                    title: event.ok == true ? L10n.string("timeline.result.done") : L10n.string("timeline.result.failed"),
                    detail: event.localizedPayloadSummaryText ?? event.localizedSummary,
                    state: event.ok == true ? .succeeded : .failed,
                    risk: nil,
                    cancellable: nil
                )
            case "error":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):error",
                    operation: event.operation,
                    title: event.code == "confirmation_required"
                        ? L10n.string("timeline.error.needs_confirmation")
                        : L10n.string("timeline.error.needs_attention"),
                    // The error box beside the timeline shows what failed.
                    detail: nil,
                    state: event.code == "confirmation_required" ? .warning : .failed,
                    risk: event.risk,
                    cancellable: event.cancellable
                )
            default:
                return nil
            }
        }
    }

    /// The item a timeline shows for the stage in progress before any of its
    /// stage events has arrived.
    static func currentStageItem(
        _ stage: OperationStageState,
        state: OperationTimelineItem.State = .running
    ) -> OperationTimelineItem {
        let note = state == .running ? longDeviceStepNote(operation: stage.operation, stage: stage.stage) : nil
        return OperationTimelineItem(
            id: "current:\(stage.operation):\(stage.stage)",
            operation: stage.operation,
            title: stageTitle(for: stage.operation, stage: stage.stage),
            detail: stage.progressDetail ?? stageDetail(for: stage.operation, stage: stage.stage),
            state: state,
            risk: stage.risk,
            cancellable: stage.cancellable,
            note: note,
            elapsedSince: note != nil ? stage.startedAt : nil
        )
    }

    /// The text of a progress event: "Files checked: N" for a migration, the
    /// fsck_hfs step for Disk Repair.
    static func progressDetail(for event: BackendEvent) -> String? {
        guard event.type == "progress" else {
            return nil
        }
        if let entries = event.entries {
            return L10n.format("timeline.progress.migration_files", entries)
        }
        if let phase = event.phase {
            return fsckPhaseDetail(phase, recheck: event.recheck == true)
        }
        return nil
    }

    /// The text of an fsck_hfs step the helper reports by name; nil for one
    /// this app has no text for, so the row keeps its last text.
    static func fsckPhaseDetail(_ phase: String, recheck: Bool) -> String? {
        let key = "timeline.fsck.phase.\(phase)"
        let step = L10n.string(key)
        guard step != key else {
            return nil
        }
        return recheck ? L10n.format("timeline.fsck.recheck", step) : step
    }

    /// "Running for 12 min", in the app's language.
    static func elapsedText(since start: Date, now: Date) -> String {
        let formatter = DateComponentsFormatter()
        formatter.allowedUnits = [.hour, .minute]
        formatter.unitsStyle = .short
        formatter.zeroFormattingBehavior = .dropLeading
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = L10n.currentLanguage.locale
        formatter.calendar = calendar
        let minutes = (max(now.timeIntervalSince(start), 0) / 60).rounded(.down)
        let duration = formatter.string(from: minutes * 60) ?? ""
        return L10n.format("timeline.fsck.elapsed", duration)
    }

    /// The last progress reported for a stage before the operation moved on.
    private static func latestProgressDetail(forStageAt index: Int, in events: [BackendEvent]) -> String? {
        let event = events[index]
        var detail: String?
        for later in events.dropFirst(index + 1) where later.operation == event.operation {
            if later.type == "stage" {
                break
            }
            if later.stage == event.stage, let text = progressDetail(for: later) {
                detail = text
            }
        }
        return detail
    }

    private static func stageState(forEventAt index: Int, in events: [BackendEvent]) -> OperationTimelineItem.State {
        let event = events[index]
        let laterEvents = events.dropFirst(index + 1).filter { $0.operation == event.operation }
        for laterEvent in laterEvents {
            switch laterEvent.type {
            case "stage":
                return .succeeded
            case "result":
                return laterEvent.ok == true ? .succeeded : .failed
            case "error" where laterEvent.code != "confirmation_required":
                return .failed
            default:
                continue
            }
        }
        return .running
    }

    static func operationTitle(_ operation: String) -> String {
        switch operation {
        case "discover":
            return L10n.string("timeline.operation.discovery")
        case "reachability":
            return L10n.string("timeline.operation.reachability")
        case "set-ssh":
            return L10n.string("timeline.operation.ssh_access")
        case "configure":
            return L10n.string("timeline.operation.configure")
        case "update-config-settings":
            return L10n.string("timeline.operation.saving_device")
        case "deploy":
            return L10n.string("timeline.operation.deploy")
        case "doctor":
            return L10n.string("timeline.operation.doctor")
        case "activate":
            return L10n.string("timeline.operation.activate")
        case "fsck":
            return L10n.string("timeline.operation.fsck")
        case "wipe-disk":
            return L10n.string("timeline.operation.wipe_disk")
        case "uninstall":
            return L10n.string("timeline.operation.uninstall")
        case "capabilities", "validate-install":
            return L10n.string("timeline.operation.readiness")
        case "set-telemetry":
            return L10n.string("timeline.operation.telemetry")
        case "version-check":
            return L10n.string("timeline.operation.version_check")
        case "flash":
            return L10n.string("timeline.operation.flash")
        default:
            return operation
        }
    }

    /// A step's title: `timeline.<operation>.<stage>.title`, else the title
    /// every operation shares for that stage, `timeline.stage.<stage>.title`
    /// (app/stage_policy.py lists the stages; tests/test_stage_titles.py checks
    /// each has one). A stage with neither reads as its code name.
    static func stageTitle(for operation: String, stage: String?) -> String {
        guard let stage else {
            return operationTitle(operation)
        }
        return stageText(operation: operation, stage: stage, part: "title")
            ?? stage
                .split(separator: "_")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
    }

    /// The step's translated detail, found as its title is; nil when it has
    /// none (the helper's English description stays in the raw log).
    static func stageDetail(for operation: String, stage: String?) -> String? {
        stage.flatMap { stageText(operation: operation, stage: $0, part: "detail") }
    }

    private static func stageText(operation: String, stage: String, part: String) -> String? {
        for key in ["timeline.\(operation).\(stage).\(part)", "timeline.stage.\(stage).\(part)"] {
            let value = L10n.string(key)
            if value != key {
                return value
            }
        }
        return nil
    }
}

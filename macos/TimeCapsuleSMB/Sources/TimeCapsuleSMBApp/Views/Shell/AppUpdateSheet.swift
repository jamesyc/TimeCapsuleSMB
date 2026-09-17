import AppKit
import SwiftUI

/// Release-notes prompt shown when a newer release exists.
struct AppUpdateSheet: View {
    let prompt: UpdatePrompt
    let isChecking: Bool
    var installState: InstallState = .idle
    /// Nil when Install Update is available; otherwise shown as the reason Download is offered instead.
    var installUnavailableReason: String? = nil
    let onDownload: () -> Void
    let onRemindLater: () -> Void
    let onSkip: () -> Void
    var onInstall: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            Divider()
            notes
            installStatus
            footer
        }
        .padding(20)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 420, idealHeight: 520)
    }

    private var canInstall: Bool {
        installUnavailableReason == nil
    }

    @ViewBuilder
    private var installStatus: some View {
        switch installState {
        case .idle:
            if let reason = installUnavailableReason {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        case .downloading(let fraction):
            VStack(alignment: .leading, spacing: 4) {
                ProgressView(value: fraction)
                Text(L10n.format("app_update.install.downloading", Int(fraction * 100)))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        case .verifying:
            installProgressRow(L10n.string("app_update.install.verifying"))
        case .installing:
            installProgressRow(L10n.string("app_update.install.installing"))
        case .readyToRelaunch:
            installProgressRow(L10n.string("app_update.install.relaunching"))
        case .failed(let failure):
            Text(failure.localizedMessage)
                .font(.caption)
                .foregroundStyle(.red)
        }
    }

    private func installProgressRow(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView()
                .controlSize(.small)
            Text(text)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable()
                .frame(width: 56, height: 56)
            VStack(alignment: .leading, spacing: 4) {
                Text(L10n.format("app_update.sheet.title", prompt.title))
                    .font(.title2.bold())
                if let date = prompt.publishedDate {
                    Text(L10n.format("app_update.sheet.published", date.formatted(date: .long, time: .omitted)))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                if prompt.isRequired {
                    Text(L10n.string("app_update.sheet.required"))
                        .font(.subheadline)
                        .foregroundStyle(.orange)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private var notes: some View {
        ScrollView {
            Group {
                if prompt.notes.isEmpty {
                    Text(L10n.string("app_update.sheet.no_notes"))
                        .foregroundStyle(.secondary)
                } else {
                    Text(ReleaseNotesMarkdown.attributedString(from: prompt.notes))
                        .textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 200)
    }

    private var footer: some View {
        HStack {
            if !prompt.isRequired {
                Button(L10n.string("app_update.sheet.skip"), action: onSkip)
                    .disabled(installState.isActive)
            }
            Spacer()
            Button(L10n.string("app_update.sheet.remind_later"), action: onRemindLater)
                .keyboardShortcut(.cancelAction)
                .disabled(installState.isActive)
            if canInstall, !isInstallFailed {
                Button(L10n.string("app_update.sheet.install"), action: onInstall)
                    .keyboardShortcut(.defaultAction)
                    .disabled(isChecking || installState.isActive)
            } else {
                Button(L10n.string("app_update.sheet.download"), action: onDownload)
                    .keyboardShortcut(.defaultAction)
                    .disabled(prompt.htmlURL == nil || isChecking || installState.isActive)
            }
        }
    }

    private var isInstallFailed: Bool {
        if case .failed = installState {
            return true
        }
        return false
    }
}

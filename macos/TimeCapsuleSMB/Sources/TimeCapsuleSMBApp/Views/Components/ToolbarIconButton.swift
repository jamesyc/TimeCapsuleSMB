import SwiftUI

struct ToolbarIconButton: View {
    let title: String
    let systemImage: String
    var disabled = false
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button {
            guard !disabled else {
                return
            }
            action()
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(disabled ? Color.secondary.opacity(0.5) : Color.primary)
                .frame(width: 28, height: 28)
                .background {
                    Circle()
                        .fill(isHovered && !disabled ? Color.primary.opacity(0.10) : Color.clear)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(disabled ? L10n.string("toolbar.disabled") : "")
        .onHover { hovering in
            isHovered = hovering
        }
    }
}

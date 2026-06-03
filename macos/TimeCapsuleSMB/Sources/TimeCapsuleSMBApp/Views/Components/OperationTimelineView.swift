import SwiftUI

struct OperationTimelineListView: View {
    let title: String?
    let emptyMessage: String?
    let items: [OperationTimelineItem]
    let showsRowBackground: Bool

    init(
        title: String? = nil,
        emptyMessage: String? = nil,
        items: [OperationTimelineItem],
        showsRowBackground: Bool = true
    ) {
        self.title = title
        self.emptyMessage = emptyMessage
        self.items = items
        self.showsRowBackground = showsRowBackground
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title)
                    .font(.headline)
            }

            if items.isEmpty {
                if let emptyMessage {
                    Text(emptyMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .transition(.opacity)
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(items) { item in
                        OperationTimelineRow(item: item, showsBackground: showsRowBackground)
                            .transition(rowTransition)
                    }
                }
            }
        }
        .animation(.snappy(duration: 0.22), value: items)
    }

    private var rowTransition: AnyTransition {
        .opacity.combined(with: .move(edge: .top))
    }
}

struct OperationTimelineRow: View {
    let item: OperationTimelineItem
    let showsBackground: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(item: OperationTimelineItem, showsBackground: Bool = true) {
        self.item = item
        self.showsBackground = showsBackground
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            OperationTimelineStateIcon(state: item.state)
                .frame(width: 22, alignment: .center)

            VStack(alignment: .leading, spacing: 2) {
                AnimatedProgressText(message: item.title, isRunning: item.state == .running && !hasDetail)
                    .font(.body.weight(.medium))
                    .foregroundStyle(.primary)
                if let detail = item.detail, !detail.isEmpty {
                    AnimatedProgressText(message: detail, isRunning: item.state == .running)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Spacer(minLength: 0)
        }
        .padding(.vertical, 5)
        .padding(.horizontal, showsBackground ? 6 : 0)
        .background(background)
        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        .contentShape(Rectangle())
        .animation(reduceMotion ? nil : .snappy(duration: 0.18), value: item.state)
        .accessibilityElement(children: .combine)
    }

    private var hasDetail: Bool {
        item.detail?.isEmpty == false
    }

    @ViewBuilder
    private var background: some View {
        if showsBackground && item.state == .running {
            OperationTimelineVisualStyle.color(for: item.state).opacity(0.10)
        } else if showsBackground && item.state == .failed {
            OperationTimelineVisualStyle.color(for: item.state).opacity(0.08)
        } else {
            Color.clear
        }
    }
}

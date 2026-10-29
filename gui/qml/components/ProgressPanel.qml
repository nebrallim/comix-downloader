import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    
    property string currentChapter: ""
    property int completedChapters: 0
    property int totalChapters: 0
    property bool isFinished: false
    property int successCount: 0
    property int failCount: 0
    
    // Theme colors
    readonly property color bgCard: "#1C1C24"
    readonly property color bgElevated: "#252530"
    readonly property color accentPrimary: "#E8A54B"
    readonly property color accentHighlight: "#FFD93D"
    readonly property color textPrimary: "#F5F5F0"
    readonly property color textSecondary: "#8B8B99"
    readonly property color success: "#7CB342"
    
    color: bgCard
    radius: 12
    
    ListModel { id: taskModel }
    
    function reset() {
        currentChapter = ""; completedChapters = 0; totalChapters = 0
        isFinished = false; successCount = 0; failCount = 0
        taskModel.clear()
    }
    
    function updateProgress(completed, total) {
        completedChapters = completed; totalChapters = total
    }
    
    function setChapterStatus(name, s, message) {
        currentChapter = name
        if (s) successCount++; else failCount++
        var found = false
        // Mark task as complete in the list
        for (var i = 0; i < taskModel.count; i++) {
            if (taskModel.get(i).name === name) {
                found = true
                taskModel.setProperty(i, "status", s ? "Complete" : "Failed")
                taskModel.setProperty(i, "progress", 100)
                if (!s && message && message.length > 0)
                    taskModel.setProperty(i, "details", message)
                break
            }
        }
        if (!found) {
            taskModel.append({
                "name": name,
                "progress": 100,
                "details": s ? "Complete" : (message || "Failed"),
                "status": s ? "Complete" : "Failed"
            })
        }
    }
    
    function updateChapterProgress(name, current, total) {
        var found = false
        for (var i = 0; i < taskModel.count; i++) {
            if (taskModel.get(i).name === name) {
                taskModel.setProperty(i, "progress", (current / total) * 100)
                taskModel.setProperty(i, "details", current + " / " + total + " images")
                found = true
                break
            }
        }
        
        if (!found) {
            taskModel.append({
                "name": name,
                "progress": (current / total) * 100,
                "details": current + " / " + total + " images",
                "status": "Downloading"
            })
        }
    }
    
    function setFinished(successful, failed) {
        isFinished = true; successCount = successful; failCount = failed
    }
    
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 8
        
        // HEADER
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: isFinished ? "DOWNLOAD COMPLETE" : "DOWNLOADING"
                font.family: "Segoe UI"; font.pixelSize: 18; font.weight: Font.DemiBold
                color: isFinished ? success : textPrimary
            }
            Item { Layout.fillWidth: true }
            Text {
                text: isFinished 
                    ? "✓ " + successCount + " succeeded" + (failCount > 0 ? ", " + failCount + " failed" : "")
                    : completedChapters + "/" + totalChapters + " chapters"
                font.pixelSize: 12
                color: isFinished ? success : textSecondary
            }
        }
        
        // OVERALL PROGRESS BAR
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 8
            color: bgElevated
            radius: 4
            
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: totalChapters > 0 ? parent.width * (completedChapters / totalChapters) : 0
                radius: 4
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: isFinished ? success : accentPrimary }
                    GradientStop { position: 1.0; color: isFinished ? "#8BC34A" : accentHighlight }
                }
                Behavior on width { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
            }
        }
        
        // ACTIVE CHAPTERS LIST
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: taskModel.count > 0
            clip: true
            
            ListView {
                id: taskListView
                model: taskModel
                spacing: 8
                delegate: ColumnLayout {
                    width: ListView.view ? ListView.view.width : (parent ? parent.width : 0)
                    spacing: 2
                    
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: model.name
                            font.pixelSize: 12; font.weight: Font.Medium
                            color: textPrimary; elide: Text.ElideRight; Layout.fillWidth: true
                        }
                        Text {
                            text: model.details
                            font.pixelSize: 10; color: textSecondary
                        }
                    }
                    
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 4
                        color: bgElevated; radius: 2
                        Rectangle {
                            height: parent ? parent.height : 0; radius: 2
                            width: parent ? parent.width * (model.progress / 100) : 0
                            color: model.status === "Failed" ? "#E57373" : (model.status === "Complete" ? success : accentPrimary)
                            Behavior on width { NumberAnimation { duration: 150 } }
                        }
                    }
                }
            }
        }
        
        // COMPLETION MESSAGE
        Text {
            text: failCount === 0 ? "🎉 All chapters downloaded successfully!" : "⚠️ Some chapters failed to download."
            font.pixelSize: 14
            color: failCount === 0 ? success : "#E57373"
            visible: isFinished
            Layout.alignment: Qt.AlignHCenter
        }
    }
}

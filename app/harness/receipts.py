"""Render only verified structured service receipts, without another model call."""


def support_receipt(data):
    labels = {"pending": "待人工受理", "in_progress": "工作人员处理中", "resolved": "已解决"}
    if not (
        data.get("delivery") == "database"
        and data.get("queue") == "human"
        and data.get("connected") is False
        and data.get("ticket_id")
        and data.get("status") in labels
    ):
        return None
    return (
        f"人工服务工单 {data['ticket_id']} 已保存，当前状态：{labels[data['status']]}。"
        "尚未接通实时人工，不保证处理时间；工单不会自动批准退款或取消订单。"
    )

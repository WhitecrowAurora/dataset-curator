"""把中心画师前缀争议返回为逐图明细，不提交有争议的归并。"""


class ReviewRequired(ValueError):
    def __init__(self, table, issues):
        reviews = list({(r['image_id'], r['part']): r for r in issues}.values())
        self.report = {'artist_reinject': dict(
            source=table['source'], origin='center', changed_tag=0, changed_nl=0,
            review_count=len(reviews), review_samples=reviews[:100])}
        super().__init__(f"中心发现 {len(reviews)} 条旧画师前缀需核对，尚未完成登记；"
                         '请核对明细后使用强制重新注入，无需启动模型')


def require_review(table, issues):
    if issues:
        raise ReviewRequired(table, issues)

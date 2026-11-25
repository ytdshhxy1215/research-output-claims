from django import forms

from apps.reports.services import KINDS, POLICIES


class FiltersForm(forms.Form):
    policy = forms.ChoiceField(label="统计口径", choices=POLICIES.items(), required=False)
    year = forms.IntegerField(label="成果事件年度", min_value=1, max_value=9999, required=False)
    kind = forms.ChoiceField(
        label="成果类型", choices=[("", "全部"), *KINDS.items()], required=False
    )
    work_id = forms.IntegerField(
        label="指定成果编号（可选）", min_value=1, max_value=2**63 - 1, required=False
    )


class DepartmentForm(forms.Form):
    department = forms.IntegerField(label="院系", min_value=1, max_value=2**63 - 1, required=False)

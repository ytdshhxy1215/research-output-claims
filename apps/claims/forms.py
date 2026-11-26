from django import forms

from apps.people.models import Department


class IdentifierSearchForm(forms.Form):
    identifier = forms.CharField(
        label="DOI 或专利文献号", max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "例如 10.5555/p1", "type": "search"}),
    )

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"]
        for prefix in (
            "https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:",
        ):
            if identifier.lower().startswith(prefix):
                identifier = identifier[len(prefix):].strip()
                break
        if not identifier:
            raise forms.ValidationError("请输入有效的成果标识。")
        return identifier


class SubmitClaimForm(forms.Form):
    evidence = forms.CharField(
        label="证据说明", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}),
        help_text="说明你与该署名如何对应。请勿上传全文或填写不必要的个人敏感信息。",
    )
    evidence_identifier = forms.CharField(
        label="可选证据标识符", max_length=255, required=False,
        help_text="例如可用于核对的公开作者标识符。",
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(
        label="理由", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}),
    )


class ReviewForm(ReasonForm):
    field_order = ("decision", "reason")
    decision = forms.ChoiceField(label="初审决定", choices=(
        ("", "请选择"), ("approve", "核准"), ("reject", "驳回"), ("conflict", "标记署名冲突"),
    ))


class AppealForm(ReasonForm):
    reason = forms.CharField(
        label="申诉补充说明", max_length=4000, widget=forms.Textarea(attrs={"rows": 5}),
        help_text="请回应初审驳回理由并补充核对线索。每条申请可提出一次申诉。",
    )


class ResolveForm(ReasonForm):
    field_order = ("decision", "reason")
    decision = forms.ChoiceField(label="复核决定", choices=(
        ("", "请选择"), ("approve", "改判核准"), ("uphold", "维持驳回"),
    ))


class ReassignForm(ReasonForm):
    field_order = ("department", "reason")
    department = forms.ModelChoiceField(
        label="改派至", queryset=Department.objects.order_by("code"),
        required=False, empty_label="校级待分派（校级可补位初审）",
    )

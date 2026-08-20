Attribute VB_Name = "RemoveChinese"
' Delete Han + CJK punctuation, keep English.
'
' Word Find with wildcards [一-龥] reports 0 hits on most Word builds
' (the wildcard engine does not treat that as a Unicode range). Use this
' macro instead: Alt+F11 → File → Import File → RemoveChinese.bas → F5
' or Insert → Module and paste everything below the Attribute line.
'
Option Explicit

Private Function Codepoint(ByVal ch As String) As Long
    Dim cp As Long
    cp = AscW(ch)
    If cp < 0 Then cp = cp + 65536
    Codepoint = cp
End Function

Private Function IsHanOrCjkPunct(ByVal cp As Long) As Boolean
    If cp >= &H4E00 And cp <= &H9FFF Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H3400 And cp <= &H4DBF Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H2E80 And cp <= &H2EFF Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H2F00 And cp <= &H2FDF Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H3001 And cp <= &H303F Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H3100 And cp <= &H312F Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &H31A0 And cp <= &H31BF Then IsHanOrCjkPunct = True: Exit Function
    If cp >= &HF900 And cp <= &HFAFF Then IsHanOrCjkPunct = True: Exit Function
    IsHanOrCjkPunct = False
End Function

Public Function StripChinese(ByVal s As String) As String
    Dim i As Long
    Dim cp As Long
    Dim ch As String
    Dim out As String
    Dim prevSpace As Boolean

    For i = 1 To Len(s)
        ch = Mid$(s, i, 1)
        ' Keep paragraph/cell/break marks so tables and layout survive.
        If ch = vbCr Or ch = vbLf Or ch = Chr$(7) Or ch = Chr$(11) Or ch = Chr$(12) Then
            out = out & ch
            prevSpace = False
        Else
            cp = Codepoint(ch)
            If cp >= &HFF01 And cp <= &HFF5E Then
                ch = ChrW(cp - &HFEE0)
                cp = Codepoint(ch)
            ElseIf cp = &H3000 Then
                ch = " "
                cp = 32
            End If
            If IsHanOrCjkPunct(cp) Then
                ' drop Chinese
            ElseIf ch = " " Or ch = vbTab Then
                If Not prevSpace Then out = out & ch
                prevSpace = True
            Else
                out = out & ch
                prevSpace = False
            End If
        End If
    Next i
    StripChinese = out
End Function

Private Function StripRangeText(ByVal rng As Range) As Long
    Dim before As String
    Dim after As String
    before = rng.Text
    after = StripChinese(before)
    If after <> before Then
        rng.Text = after
        StripRangeText = Len(before) - Len(after)
    End If
End Function

Private Function StripParagraphs(ByVal story As Range) As Long
    Dim p As Paragraph
    Dim r As Range
    Dim n As Long
    Dim chEnd As String
    For Each p In story.Paragraphs
        Set r = p.Range
        If Len(r.Text) > 0 Then
            Do While r.End > r.Start
                chEnd = Right$(r.Text, 1)
                If chEnd = vbCr Or chEnd = Chr$(7) Then
                    r.End = r.End - 1
                Else
                    Exit Do
                End If
            Loop
            If r.End > r.Start Then n = n + StripRangeText(r)
        End If
    Next p
    StripParagraphs = n
End Function

Private Function StripShapes(ByVal shps As Shapes) As Long
    Dim shp As Shape
    Dim n As Long
    Dim hasText As Boolean
    For Each shp In shps
        hasText = False
        On Error Resume Next
        hasText = shp.TextFrame.HasText
        On Error GoTo 0
        If hasText Then n = n + StripRangeText(shp.TextFrame.TextRange)
    Next shp
    StripShapes = n
End Function

Private Function StripHeadersFooters() As Long
    Dim sec As Section
    Dim hf As HeaderFooter
    Dim n As Long
    For Each sec In ActiveDocument.Sections
        For Each hf In sec.Headers
            If hf.Exists Then
                n = n + StripParagraphs(hf.Range)
                n = n + StripShapes(hf.Shapes)
            End If
        Next hf
        For Each hf In sec.Footers
            If hf.Exists Then
                n = n + StripParagraphs(hf.Range)
                n = n + StripShapes(hf.Shapes)
            End If
        Next hf
    Next sec
    StripHeadersFooters = n
End Function

Public Sub RemoveChineseKeepEnglish()
    Dim n As Long
    Dim hadTrack As Boolean
    Dim notes As Range

    If Documents.Count = 0 Then
        MsgBox "Open a document first.", vbExclamation
        Exit Sub
    End If

    Application.ScreenUpdating = False
    hadTrack = ActiveDocument.TrackRevisions
    ActiveDocument.TrackRevisions = False

    n = StripParagraphs(ActiveDocument.Content)
    n = n + StripShapes(ActiveDocument.Shapes)
    n = n + StripHeadersFooters()

    On Error Resume Next
    Set notes = ActiveDocument.StoryRanges(wdFootnotesStory)
    If Not notes Is Nothing Then n = n + StripParagraphs(notes)
    Set notes = ActiveDocument.StoryRanges(wdEndnotesStory)
    If Not notes Is Nothing Then n = n + StripParagraphs(notes)
    Set notes = ActiveDocument.StoryRanges(wdCommentsStory)
    If Not notes Is Nothing Then n = n + StripParagraphs(notes)
    On Error GoTo 0

    ActiveDocument.TrackRevisions = hadTrack
    Application.ScreenUpdating = True

    If n = 0 Then
        MsgBox "No Chinese characters were removed." & vbCrLf & vbCrLf & _
               "If you still see Chinese: it may be an image, or Find wildcards " & _
               "([一-龥]) were used — those report 0 on English Word. Run this macro instead.", _
               vbInformation
    Else
        MsgBox "Removed " & n & " Chinese character(s). English was kept.", vbInformation
    End If
End Sub

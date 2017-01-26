function display_preview_render(e){
    var src = $(e.target).attr('img_src');
    var img = '<img src="' + src + '" class="img-responsive"/>';
    $('#myModal').modal();
    $('#myModal').on('shown.bs.modal', function(){
        $('#myModal .modal-body').html(img);
    });
    $('#myModal').on('hidden.bs.modal', function(){
        $('#myModal .modal-body').html('');
    });   
}

function wire_preview_buttons(){
    let buttons = $('div.renderimg')
    for( let i = 0; i < buttons.length; i++){
	$(buttons[i]).on('click', display_preview_render );
    }
}

$(document).ready(function(){
    wire_preview_buttons();

    $('#jobstatus-table').on('post-body.bs.table', function (e, name, order) {
	wire_preview_buttons();
    });
    
    $('div.requeue_btn').on('click',function(){
        $.ajax( {
            url: $(this).attr('target'),
            type: 'GET',
            processData: false,
            contentType: false,
            complete: function() {
                $('#frame-row-'+$(this).attr('uuid')+' .frame-tbl-complete').text("NO");
                $('#frame-row-'+$(this).attr('uuid')+' .frame-tbl-status').text("WAITING");
                $('#frame-row-'+$(this).attr('uuid')+' .frame-tbl-time').text("");
                $('#frame-row-'+$(this).attr('uuid')+' .frame-tbl-actions').text("");
            }.bind(this)
        } );
    });

    $( '#form-manual_upload' )
	.submit( function( e ) {
            var data = new FormData($('#form-manual_upload')[0]);
            data.append( "job_uuid",  "{{ job_data.uuid }}" );
            console.log( data )
            $.ajax( {
                url: '/manual_upload',
                type: 'POST',
                data: data,
                processData: false,
                contentType: false
            } );
            e.preventDefault();
            $('#manual-submit-form').modal('hide');
	}.bind(this) );


})
